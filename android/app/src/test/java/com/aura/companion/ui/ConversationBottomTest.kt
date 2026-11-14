package com.aura.companion.ui

import org.junit.Assert.*
import org.junit.Test

class ConversationBottomTest {
    @Test fun aVisibleLongLastMessageIsNotTheBottom() {
        assertFalse(isNearConversationBottom(8, 10, 2500, 800, 24))
        assertFalse(isNearConversationBottom(9, 10, 2500, 800, 24))
    }
    @Test fun actualTailPositionAllowsOnlySmallPixelTolerance() {
        assertTrue(isNearConversationBottom(9, 10, 810, 800, 24))
        assertFalse(isNearConversationBottom(9, 10, 825, 800, 24))
        assertTrue(isNearConversationBottom(9, 10, 700, 800, 24))
        assertFalse(isNearConversationBottom(-1, 0, 0, 800, 24))
    }
}
