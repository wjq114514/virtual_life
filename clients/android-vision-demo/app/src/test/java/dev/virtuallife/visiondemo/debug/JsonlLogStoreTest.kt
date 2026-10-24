package dev.virtuallife.visiondemo.debug

import java.nio.file.Files
import org.junit.Assert.*
import org.junit.Test

class JsonlLogStoreTest {
    @Test fun contentIsFilteredAndNumbersRemainStructured() {
        val directory = Files.createTempDirectory("vision-log-test").toFile()
        try {
            val store = JsonlLogStore(directory)
            val line = store.append("capture_complete", mapOf(
                "bytes" to 123, "capture_ms" to 1.25, "prompt" to "SECRET",
                "text" to "SECRET", "reason" to "unchanged", "error_type" to "IOException",
                "status" to "SECRET with spaces", "custom" to "SECRET", "ratio" to Double.NaN,
            ), 1234)
            assertTrue(line.contains("\"bytes\":123"))
            assertTrue(line.contains("\"elapsed_ns\":1234"))
            assertTrue(line.contains("\"reason\":\"unchanged\""))
            assertFalse(line.contains("SECRET"))
            assertFalse(line.contains("NaN"))
            assertEquals(line + "\n", store.tail())
        } finally { directory.deleteRecursively() }
    }

    @Test fun rotationTailAndSnapshotStayBounded() {
        val directory = Files.createTempDirectory("vision-log-test").toFile()
        try {
            val store = JsonlLogStore(directory, 1024, 3, 200)
            repeat(100) { store.append("capture_complete", mapOf("sequence" to it), it.toLong()) }
            val logs = directory.listFiles()!!.filter { it.name.matches(Regex("vision-[0-9]+.jsonl")) }
            assertTrue(logs.size <= 3)
            assertTrue(logs.all { it.length() <= 1024 })
            val tail = store.tail()
            assertTrue(tail.toByteArray().size <= 200)
            assertTrue(tail.lines().filter { it.isNotEmpty() }.all { it.startsWith("{") && it.endsWith("}") })
            val exported = store.exportFile()
            val snapshot = exported.readText()
            store.append("later", emptyMap(), 101)
            assertEquals(snapshot, exported.readText())
            assertTrue(exported.length() <= 3 * 1024)
            repeat(3) { store.exportFile() }
            assertEquals(1, directory.listFiles()!!.count { it.name == "vision-export.jsonl" })
        } finally { directory.deleteRecursively() }
    }
}
