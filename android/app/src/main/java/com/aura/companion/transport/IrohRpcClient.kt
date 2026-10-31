package com.aura.companion.transport

import android.content.Context
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
    private val permits = Semaphore(RpcProtocol.MAX_CONCURRENT_REQUESTS)
    @Volatile private var connectingJob: Job? = null
    @Volatile private var session: Session? = null
    @Volatile var endpointId: String = ""
        private set
    @Volatile var connectionType: String = "unknown"
        private set

    private class Session(val endpoint: Endpoint, val connection: Connection) {
        val job = SupervisorJob()
        val scope = CoroutineScope(job + Dispatchers.IO)
    }

    suspend fun connect(ticket: String): Unit = withContext(Dispatchers.IO) {
        val epoch = closeEpoch.get()
        lifecycle.withLock {
            connectingJob = currentCoroutineContext()[Job]
            try {
                if (epoch != closeEpoch.get()) throw CancellationException("Connection cancelled")
                connectLocked(ticket)
            } finally {
                connectingJob = null
            }
        }
    }

    private suspend fun connectLocked(ticket: String) {
        if (ticket.isBlank() || ticket.length > 16 * 1024) {
            throw RpcTransportException("invalid_ticket", "电脑连接码无效，请重新扫描或复制。")
        }
        val destination = try {
            // JNI DNS initialization must precede Endpoint construction.
            IrohAndroid.installAndroidContext(app)
            EndpointTicket.fromString(ticket.trim())
        } catch (_: Exception) {
            throw RpcTransportException("invalid_ticket", "电脑连接码无效，请重新扫描或复制。")
        } catch (_: LinkageError) {
            throw RpcTransportException("native_unavailable", "此设备无法加载加密通信组件。")
        }
        var endpoint: Endpoint? = null
        var connection: Connection? = null
        try {
            detachAndClose()
            withTimeout(RpcProtocol.TIMEOUT_MILLIS) {
                val key = identityStore.loadOrCreate()
                try {
                    EndpointBuilder().use { builder ->
                        builder.applyN0()
                        builder.secretKey(key)
                        builder.alpns(listOf(RpcProtocol.ALPN))
                        endpoint = builder.bind()
                    }
                } finally {
                    key.fill(0)
                }
                destination.endpointAddr().use { address ->
                    connection = endpoint!!.connect(address, RpcProtocol.ALPN)
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
                connection!!.setReceiveWindow(256uL * 1024uL)
                connection!!.setMaxConcurrentUniStreams(0uL)
                // A client never accepts server-initiated RPC streams.
                connection!!.setMaxConcurrentBiStreams(0uL)
                endpointId = endpoint!!.id().use { it.toString() }
                session = Session(endpoint!!, connection!!)
                connectionType = selectedPath(connection!!)
                endpoint = null
                connection = null
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
        val active = session ?: throw RpcTransportException("not_connected", "请先连接电脑。")
        val task = active.scope.async {
            withTimeout(RpcProtocol.TIMEOUT_MILLIS) {
                permits.withPermit {
                    if (session !== active) throw RpcTransportException("disconnected", "电脑连接已关闭，请重新连接。")
                    val payload = RpcProtocol.encode(RpcProtocol.request(method, params, requestId))
                    active.connection.openBi().use { stream ->
                        stream.send().use { send ->
                            stream.recv().use { recv ->
                                var outboundFinished = false
                                try {
                                    val response = supervisorScope {
                                        val writer = async {
                                            send.writeAll(RpcProtocol.frameHeader(payload.size))
                                            send.writeAll(payload)
                                            send.finish()
                                            outboundFinished = true
                                        }
                                        try {
                                            val length = RpcProtocol.frameLength(recv.readExact(4u))
                                            val value = RpcProtocol.validateResponse(
                                                RpcProtocol.decode(recv.readExact(length.toUInt())), requestId)
                                            if (recv.read(1u).isNotEmpty()) {
                                                throw RpcTransportException("invalid_frame", "电脑返回了多余的消息数据。")
                                            }
                                            if (value.getBoolean("ok")) writer.await()
                                            value
                                        } finally {
                                            writer.cancelAndJoin()
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
            task.await()
        } catch (error: RpcTransportException) {
            throw error
        } catch (_: TimeoutCancellationException) {
            throw RpcTransportException("timeout", "电脑响应超时，请重新连接后重试。")
        } catch (error: CancellationException) {
            throw error
        } catch (_: Exception) {
            throw RpcTransportException("connection_failed", "通信中断，请重新连接电脑。")
        } finally {
            task.cancel()
        }
    }

    /** Suspends off the UI thread; releases sockets and all native handles. */
    suspend fun close(): Unit = withContext(Dispatchers.IO) {
        // Disconnect also interrupts a connection attempt instead of waiting
        // behind its full handshake timeout while holding the lifecycle lock.
        closeEpoch.incrementAndGet()
        connectingJob?.cancel()
        lifecycle.withLock { detachAndClose() }
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
