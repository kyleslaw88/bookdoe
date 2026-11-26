package bookdoe.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.longOrNull
import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException

/** Lenient readers over dynamic JSON (servers are not always consistent about types). */
object JsonHelpers {
    private fun prim(obj: JsonObject, key: String): JsonPrimitive? = (obj[key] as? JsonPrimitive)?.takeIf { it !is JsonNull }

    fun string(obj: JsonObject, key: String): String? = prim(obj, key)?.content

    fun double(obj: JsonObject, key: String): Double? = prim(obj, key)?.let { it.doubleOrNull ?: it.content.toDoubleOrNull() }

    fun int(obj: JsonObject, key: String): Int? = prim(obj, key)?.let { it.intOrNull ?: it.doubleOrNull?.toInt() ?: it.content.toIntOrNull() }

    fun long(obj: JsonObject, key: String): Long? = prim(obj, key)?.let { it.longOrNull ?: it.doubleOrNull?.toLong() ?: it.content.toLongOrNull() }

    fun bool(obj: JsonObject, key: String): Boolean? = prim(obj, key)?.let { it.booleanOrNull ?: it.intOrNull?.let { n -> n != 0 } }

    fun obj(obj: JsonObject, key: String): JsonObject? = obj[key] as? JsonObject

    fun array(obj: JsonObject, key: String): List<JsonObject> = (obj[key] as? JsonArray)?.mapNotNull { it as? JsonObject } ?: emptyList()

    fun stringArray(obj: JsonObject, key: String): List<String>? =
        (obj[key] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.takeIf { p -> p !is JsonNull }?.content }

    fun objects(array: JsonArray): List<JsonObject> = array.mapNotNull { it as? JsonObject }

    /** Epoch seconds (possibly fractional) to epoch milliseconds. */
    fun millisFromSeconds(seconds: Double?): Long? = seconds?.let { (it * 1000).toLong() }

    /** ISO 8601 to epoch milliseconds. */
    fun millisFromIso(iso: String?): Long? {
        if (iso == null) return null
        return try {
            OffsetDateTime.parse(iso).toInstant().toEpochMilli()
        } catch (e: DateTimeParseException) {
            try {
                Instant.parse(iso).toEpochMilli()
            } catch (e2: DateTimeParseException) {
                null
            }
        }
    }

    fun element(obj: JsonObject, key: String): JsonElement? = obj[key]?.takeIf { it !is JsonNull }
}
