package com.aura.companion.data

import android.util.Base64
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class PairingInputTest {
    private val now = 1_800_000_000_000L
    private val secret = "DO_NOT_ECHO_THIS_SECRET"
    private fun valid() = JSONObject().put("ticket", "public-ticket").put("code", secret)
        .put("endpoint_id", "desktop").put("version", 1).put("expires_at", now / 1000.0 + 300)
    private fun uri(value: String, padded: Boolean = false): String = "aura://pair/" + Base64.encodeToString(
        value.toByteArray(Charsets.UTF_8), Base64.URL_SAFE or Base64.NO_WRAP or (if (padded) 0 else Base64.NO_PADDING))
    private fun rejected(value: String, expected: String) {
        val error = assertThrows(PairingInputException::class.java) { PairingInput.parse(value, now) }
        assertEquals(expected, error.message)
        assertFalse(error.message.orEmpty().contains(secret))
        assertNull(error.cause)
    }

    @Test fun bareShortCodeExplainsWhichDesktopButtonToUse() {
        rejected("Q".repeat(24), PairingInput.SHORT_CODE)
        rejected(secret, PairingInput.SHORT_CODE)
        assertTrue(PairingInput.SHORT_CODE.contains("复制配对信息"))
        assertTrue(PairingInput.SHORT_CODE.contains("整段"))
    }

    @Test fun emptyAndOversizedInputsGiveDifferentActionableErrors() {
        rejected(" \n\t", PairingInput.EMPTY)
        rejected("Q".repeat(32769), PairingInput.TOO_LONG)
    }

    @Test fun malformedJsonAndTrailingInputNeverExposeThePastedSecret() {
        for (input in listOf("{\"ticket\":\"$secret\"", "{not-json", "[\"$secret\"]",
            "{ticket:'$secret',code:'abc'}", valid().toString() + " trailing-$secret")) {
            rejected(input, PairingInput.FORMAT)
        }
    }

    @Test fun missingAddressAndMissingCodeHaveSpecificInstructions() {
        rejected(JSONObject().put("code", secret).toString(), PairingInput.MISSING_TICKET)
        rejected(JSONObject().put("ticket", "public-ticket").toString(), PairingInput.MISSING_CODE)
    }

    @Test fun wrongTypesAndDuplicateKeysAreNotCoercedIntoConnectableCredentials() {
        rejected(valid().put("ticket", 123).toString(), PairingInput.FIELDS)
        rejected(valid().put("code", true).toString(), PairingInput.FIELDS)
        rejected(valid().put("expires_at", "later").toString(), PairingInput.FIELDS)
        rejected(valid().put("pairing", "not-an-object").toString(), PairingInput.FIELDS)
        rejected("{\"ticket\":\"public-ticket\",\"code\":\"abc\",\"code\":\"$secret\"}", PairingInput.FIELDS)
    }

    @Test fun invalidOrIncompatibleVersionsAreRejectedInsideEveryEnvelope() {
        rejected(valid().put("version", 2).toString(), PairingInput.VERSION)
        rejected(valid().put("version", "1").toString(), PairingInput.VERSION)
        rejected(JSONObject().put("version", 2).put("pairing", valid()).toString(), PairingInput.VERSION)
        rejected(uri(valid().put("version", 2).toString()), PairingInput.VERSION)
    }

    @Test fun expiredJsonAndUriAreRejectedBeforeAnyNetworkConnection() {
        val expired = valid().put("expires_at", now / 1000.0 - 1)
        rejected(expired.toString(), PairingInput.EXPIRED)
        rejected(uri(expired.toString()), PairingInput.EXPIRED)
        rejected(JSONObject().put("pairing", JSONObject().put("uri", uri(expired.toString()))).toString(), PairingInput.EXPIRED)
    }

    @Test fun malformedBase64NonJsonAndInvalidUtf8LinksStaySafe() {
        for (input in listOf("aura://wrong/abc", "aura://pair/", "aura://pair/%%%$secret", "aura://pair/A",
            "aura://pair/Zh", uri("not-json-$secret"), "aura://pair/_w")) {
            rejected(input, PairingInput.LINK)
        }
    }

    @Test fun fullDirectNestedAndEncodedPairingInformationAreSupported() {
        val json = valid()
        for (input in listOf(json.toString(), "\uFEFF  " + json.toString(2),
            JSONObject().put("pairing", json).toString(), uri(json.toString()), uri(json.toString(), true),
            JSONObject().put("pairing", JSONObject().put("uri", uri(json.toString()))).toString(),
            JSONObject().put("code", secret).put("ticket", "public-ticket").put("uri", uri(json.toString())).toString())) {
            val parsed = PairingInput.parse(input, now)
            assertEquals("public-ticket", parsed.getString("ticket"))
            assertEquals(secret, parsed.getString("code"))
            assertEquals("desktop", parsed.getString("endpoint_id"))
            assertEquals(1, parsed.getInt("version"))
            assertFalse(parsed.has("uri"))
        }
    }

    @Test fun mismatchedCopiedEnvelopesAreNotSilentlyCombined() {
        val input = valid().put("uri", uri(valid().put("code", "different-code").toString()))
        rejected(input.toString(), PairingInput.CONFLICT)
    }

    @Test fun fieldLengthsControlCharactersAndWhitespaceAreBounded() {
        rejected(valid().put("ticket", "public ticket").toString(), PairingInput.FIELDS)
        rejected(valid().put("ticket", "Q".repeat(16385)).toString(), PairingInput.FIELDS)
        rejected(valid().put("code", "Q".repeat(129)).toString(), PairingInput.FIELDS)
        rejected(valid().put("endpoint_id", "desktop\u007f").toString(), PairingInput.FIELDS)
    }
}
