package com.aura.companion.transport

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure framing tests: no fake SDK endpoints, network or Android services. */
class RpcProtocolTest {
    @Test fun frameUsesUnsignedBigEndianLength() {
        assertArrayEquals(byteArrayOf(0, 0, 1, 0), RpcProtocol.frameHeader(256))
        assertEquals(256, RpcProtocol.frameLength(byteArrayOf(0, 0, 1, 0)))
        assertEquals(RpcProtocol.MAX_FRAME_BYTES,
            RpcProtocol.frameLength(RpcProtocol.frameHeader(RpcProtocol.MAX_FRAME_BYTES)))
    }

    @Test fun lengthIsRejectedBeforeNativeAllocation() {
        listOf(byteArrayOf(0, 0, 0, 0), byteArrayOf(-1, -1, -1, -1),
            byteArrayOf(0, -128, 0, 1), byteArrayOf(0, 1)).forEach { header ->
            try {
                RpcProtocol.frameLength(header)
                throw AssertionError("Unbounded or incomplete header accepted")
            } catch (error: RpcTransportException) {
                assertTrue(error.code in setOf("frame_too_large", "invalid_frame"))
            }
        }
    }

    @Test fun imageLimitFitsWireEnvelope() {
        val base64Length = ((4 * 1024 * 1024 + 2) / 3) * 4
        assertTrue(base64Length + 1024 < RpcProtocol.MAX_FRAME_BYTES)
    }

    @Test fun requestIdsMustBeCanonicalForPersistentDeduplication() {
        assertTrue(RpcProtocol.canonicalUuid("12345678-1234-4321-8000-123456789abc"))
        assertFalse(RpcProtocol.canonicalUuid("12345678-1234-4321-8000-123456789ABC"))
        assertFalse(RpcProtocol.canonicalUuid("1-2-3-4-5"))
        assertFalse(RpcProtocol.canonicalUuid("not-an-id"))
    }
}
