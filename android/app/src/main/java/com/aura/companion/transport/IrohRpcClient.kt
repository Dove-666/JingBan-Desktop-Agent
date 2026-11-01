package com.aura.companion.transport

import android.content.Context
import android.util.Log
import computer.iroh.Connection
import computer.iroh.Endpoint
import computer.iroh.EndpointBuilder
import computer.iroh.EndpointTicket
import computer.iroh.IrohAndroid
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.supervisorScope
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.atomic.AtomicLong

/**
 * Real iroh 1.2.0 client. connect/request/close are suspend functions and move
 * native and Keystore calls onto Dispatchers.IO. A session owns all its request
 * jobs; reconnect/close cancels and closes that session before replacing it.
 */
class IrohRpcClient(
    context: Context,
    private val identityStore: DeviceIdentityStore = DeviceIdentityStore(context),
) {
    private val app = context.applicationContext
    private val lifecycle = Mutex()
    private val closeEpoch = AtomicLong()
    private val diagnosticSequence = AtomicLong()
    private val connectAttempts = AtomicLong()
    private val endpointBinds = AtomicLong()
    private val reusedConnections = AtomicLong()
    private val closeAttempts = AtomicLong()
    private val permits = Semaphore(RpcProtocol.MAX_CONCURRENT_REQUESTS)
    @Volatile private var connectingJob: Job? = null
    @Volatile private var session: Session? = null
    @Volatile var endpointId: String = ""
        private set
    @Volatile var connectionType: String = "unknown"
        private set

    private class Session(val endpoint: Endpoint, val connection: Connection, val ticket: String) {
        val job = SupervisorJob()
        val scope = CoroutineScope(job + Dispatchers.IO)
        @Volatile var reusable = true
    }

    internal fun connectionMetrics() = NativeConnectionMetrics(
        connectAttempts.get(), endpointBinds.get(), reusedConnections.get())

    /** opt-in AURAConnect: op 1=connect, 2=close; all field values are numeric. */
    private fun timing(operation: Int, attempt: Long): ConnectionTiming? {
        if (!Log.isLoggable("AURAConnect", Log.DEBUG)) return null
        return ConnectionTiming { phase, elapsed ->
            Log.d("AURAConnect", "op=$operation attempt=$attempt phase=$phase elapsed_ms=$elapsed")
        }
    }

    /** Explicit local opt-in; never logs peer IDs, request IDs, tickets or payloads. */
    private fun trace(sequence: Long, method: String, stage: String) {
        if (!Log.isLoggable("AURARemote", Log.DEBUG)) return
        val label = when (method) {
            "pair", "status", "sync.snapshot", "sync.pull", "operation.get", "chat.send", "chat.config", "chat.import",
            "image.upload", "image.list", "image.get", "image.delete", "pet.command" -> method
            else -> "other"
        }
        Log.d("AURARemote", "rpc=$sequence method=$label stage=$stage")
    }

    suspend fun connect(ticket: String) {
        val epoch = closeEpoch.get()
        val measurement = timing(1, connectAttempts.incrementAndGet())
        measurement?.mark(ConnectionPhase.START)
        try {
            withContext(Dispatchers.IO) {
                lifecycle.withLock {
                    measurement?.mark(ConnectionPhase.LOCK_ACQUIRED)
                    connectingJob = currentCoroutineContext()[Job]
                    try {
                        if (epoch != closeEpoch.get()) throw CancellationException("Connection cancelled")
                        connectLocked(ticket, measurement)
                    } finally {
                        connectingJob = null
                    }
                }
            }
        } catch (error: CancellationException) {
            measurement?.mark(ConnectionPhase.CANCELLED)
            throw error
        } catch (error: Exception) {
            measurement?.mark(ConnectionPhase.FAILED)
            throw error
        } finally {
            measurement?.mark(ConnectionPhase.FINISHED)
        }
    }

    private suspend fun connectLocked(ticket: String, measurement: ConnectionTiming?) {
        if (ticket.isBlank() || ticket.length > 16 * 1024) {
            throw RpcTransportException("invalid_ticket", "电脑连接码无效，请重新扫描或复制。")
        }
        val normalizedTicket = ticket.trim()
        val current = session
        // Identity and ALPN were verified before this session was published.
        // A different ticket always takes the full authentication path. A
        // timed-out/failed session is never reused, even if QUIC has not yet
        // detected the dead peer in closeReason(). No business RPC is replayed.
        val reusable = current != null && current.ticket == normalizedTicket && current.reusable && current.job.isActive &&
            try { !current.endpoint.isClosed() && current.connection.closeReason() == null }
            catch (_: Exception) { false }
            catch (_: LinkageError) { false }
        if (reusable) {
            reusedConnections.incrementAndGet()
            measurement?.mark(ConnectionPhase.REUSED)
            return
        }
        val destination = try {
            // JNI DNS initialization must precede Endpoint construction.
            IrohAndroid.installAndroidContext(app)
            measurement?.mark(ConnectionPhase.CONTEXT_READY)
            EndpointTicket.fromString(normalizedTicket).also { measurement?.mark(ConnectionPhase.TICKET_PARSED) }
        } catch (_: Exception) {
            throw RpcTransportException("invalid_ticket", "电脑连接码无效，请重新扫描或复制。")
        } catch (_: LinkageError) {
            throw RpcTransportException("native_unavailable", "此设备无法加载加密通信组件。")
        }
        var endpoint: Endpoint? = null
        var connection: Connection? = null
        try {
            detachAndClose()
            measurement?.mark(ConnectionPhase.PREVIOUS_CLOSED)
            withTimeout(RpcProtocol.TIMEOUT_MILLIS) {
                val key = identityStore.loadOrCreate()
                measurement?.mark(ConnectionPhase.IDENTITY_LOADED)
                try {
                    EndpointBuilder().use { builder ->
                        builder.applyN0()
                        builder.secretKey(key)
                        builder.alpns(listOf(RpcProtocol.ALPN))
                        endpoint = builder.bind()
                        endpointBinds.incrementAndGet()
                        measurement?.mark(ConnectionPhase.ENDPOINT_BOUND)
                    }
                } finally {
                    key.fill(0)
                }
                destination.endpointAddr().use { address ->
                    connection = endpoint!!.connect(address, RpcProtocol.ALPN)
                    measurement?.mark(ConnectionPhase.HANDSHAKE_COMPLETE)
                    address.id().use { expected ->
                        connection!!.remoteId().use { authenticated ->
                            if (!MessageDigest.isEqual(expected.toBytes(), authenticated.toBytes())) {
                                throw RpcTransportException("identity_mismatch", "电脑的加密身份不匹配，连接已拒绝。")
                            }
                        }
                    }
                }
                if (!connection!!.alpn().contentEquals(RpcProtocol.ALPN)) {
                    throw RpcTransportException("protocol_mismatch", "电脑通信协议不兼容，请更新应用。")
                }
                measurement?.mark(ConnectionPhase.AUTHENTICATED)
                connection!!.setReceiveWindow(256uL * 1024uL)
                connection!!.setMaxConcurrentUniStreams(0uL)
                // A client never accepts server-initiated RPC streams.
                connection!!.setMaxConcurrentBiStreams(0uL)
                endpointId = endpoint!!.id().use { it.toString() }
                session = Session(endpoint!!, connection!!, normalizedTicket)
                connectionType = selectedPath(connection!!)
                endpoint = null
                connection = null
                measurement?.mark(ConnectionPhase.READY)
            }
        } catch (error: RpcTransportException) {
            throw error
        } catch (_: TimeoutCancellationException) {
            throw RpcTransportException("timeout", "连接电脑超时，请确认电脑已开启远程连接。")
        } catch (error: CancellationException) {
            throw error
        } catch (_: Exception) {
            throw RpcTransportException("connection_failed", "无法连接电脑，请检查两端网络和连接码后重试。")
        } catch (_: LinkageError) {
            throw RpcTransportException("native_unavailable", "此设备无法加载加密通信组件。")
        } finally {
            destination.close()
            closeNative(connection, endpoint)
        }
    }

    /** requestId is supplied/persisted by the repository for safe reconnect retries. */
    suspend fun request(
        method: String,
        params: JSONObject = JSONObject(),
        requestId: String = UUID.randomUUID().toString(),
    ): JSONObject = withContext(Dispatchers.IO) {
        val sequence = diagnosticSequence.incrementAndGet()
        trace(sequence, method, "request")
        val active = session ?: throw RpcTransportException("not_connected", "请先连接电脑。")
        val task = active.scope.async {
            withTimeout(RpcProtocol.TIMEOUT_MILLIS) {
                trace(sequence, method, "permit_wait")
                permits.withPermit {
                    trace(sequence, method, "permit_acquired")
                    if (session !== active) throw RpcTransportException("disconnected", "电脑连接已关闭，请重新连接。")
                    val payload = RpcProtocol.encode(RpcProtocol.request(method, params, requestId))
                    trace(sequence, method, "encoded")
                    active.connection.openBi().use { stream ->
                        trace(sequence, method, "stream_open")
                        stream.send().use { send ->
                            stream.recv().use { recv ->
                                var outboundFinished = false
                                try {
                                    val response = supervisorScope {
                                        val writer = async {
                                            send.writeAll(RpcProtocol.frameHeader(payload.size))
                                            trace(sequence, method, "header_sent")
                                            if (payload.size > 1024) {
                                                trace(sequence, method, "paced_body")
                                                // Avoid batching the whole image into one native write.
                                                // Small RPCs keep a single write; large frames yield to
                                                // the native sender between bounded chunks.
                                                var offset = 0
                                                while (offset < payload.size) {
                                                    val end = minOf(offset + 1024, payload.size)
                                                    send.writeAll(payload.copyOfRange(offset, end))
                                                    offset = end
                                                    if (offset < payload.size) delay(2)
                                                }
                                            } else {
                                                trace(sequence, method, "bulk_body")
                                                send.writeAll(payload)
                                            }
                                            trace(sequence, method, "body_sent")
                                            send.finish()
                                            outboundFinished = true
                                            trace(sequence, method, "send_finished")
                                        }
                                        try {
                                            trace(sequence, method, "response_wait")
                                            val length = RpcProtocol.frameLength(recv.readExact(4u))
                                            trace(sequence, method, "response_header")
                                            val value = RpcProtocol.validateResponse(
                                                RpcProtocol.decode(recv.readExact(length.toUInt())), requestId)
                                            trace(sequence, method, "response_body")
                                            if (recv.read(1u).isNotEmpty()) {
                                                throw RpcTransportException("invalid_frame", "电脑返回了多余的消息数据。")
                                            }
                                            if (value.getBoolean("ok")) writer.await()
                                            trace(sequence, method, "response_finished")
                                            value
                                        } finally {
                                            trace(sequence, method, "writer_cleanup")
                                            writer.cancelAndJoin()
                                            trace(sequence, method, "writer_released")
                                        }
                                    }
                                    if (session === active) connectionType = selectedPath(active.connection)
                                    response
                                } finally {
                                    withContext(NonCancellable) {
                                        withTimeoutOrNull(1_000) {
                                            try { recv.stop(0uL) } catch (_: Exception) { }
                                            if (!outboundFinished) try { send.reset(0uL) } catch (_: Exception) { }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        try {
            task.await().also { trace(sequence, method, "complete") }
        } catch (error: RpcTransportException) {
            active.reusable = false
            trace(sequence, method, "protocol_error")
            throw error
        } catch (_: TimeoutCancellationException) {
            active.reusable = false
            trace(sequence, method, "timeout")
            throw RpcTransportException("timeout", "电脑响应超时，请重新连接后重试。")
        } catch (error: CancellationException) {
            trace(sequence, method, "cancelled")
            throw error
        } catch (_: Exception) {
            active.reusable = false
            trace(sequence, method, "connection_error")
            throw RpcTransportException("connection_failed", "通信中断，请重新连接电脑。")
        } finally {
            task.cancel()
        }
    }

    /** Suspends off the UI thread; releases sockets and all native handles. */
    suspend fun close() {
        val measurement = timing(2, closeAttempts.incrementAndGet())
        measurement?.mark(ConnectionPhase.START)
        try {
            withContext(Dispatchers.IO) {
                // Disconnect also interrupts a connection attempt instead of waiting
                // behind its full handshake timeout while holding the lifecycle lock.
                closeEpoch.incrementAndGet()
                connectingJob?.cancel()
                lifecycle.withLock {
                    measurement?.mark(ConnectionPhase.LOCK_ACQUIRED)
                    detachAndClose()
                    measurement?.mark(ConnectionPhase.PREVIOUS_CLOSED)
                }
            }
        } finally {
            measurement?.mark(ConnectionPhase.FINISHED)
        }
    }

    private suspend fun detachAndClose() {
        val previous = session
        session = null
        endpointId = ""
        connectionType = "unknown"
        if (previous == null) return
        previous.job.cancel()
        // Closing the connection also wakes suspended native stream reads.
        try { previous.connection.close(0L, byteArrayOf()) } catch (_: Exception) { }
        withContext(NonCancellable) {
            withTimeoutOrNull(5_000) { previous.job.join() }
            closeNative(previous.connection, previous.endpoint)
        }
    }

    private suspend fun closeNative(connection: Connection?, endpoint: Endpoint?) {
        if (connection == null && endpoint == null) return
        withContext(NonCancellable + Dispatchers.IO) {
            try { connection?.close(0L, byteArrayOf()) } catch (_: Exception) { }
            withTimeoutOrNull(5_000) {
                try { endpoint?.shutdown() } catch (_: Exception) { }
            }
            try { connection?.close() } catch (_: Exception) { }
            try { endpoint?.close() } catch (_: Exception) { }
        }
    }

    private fun selectedPath(connection: Connection): String = try {
        val paths = connection.paths().filter { it.isSelected }
        when {
            paths.isNotEmpty() && paths.all { it.isRelay } -> "relay"
            paths.isNotEmpty() && paths.all { it.isIp && !it.isRelay } -> "direct"
            else -> "unknown"
        }
    } catch (_: Exception) { "unknown" }
}
