# kotlinx.serialization: keep generated serializers for the persisted models.
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-keepclassmembers class bookdoe.** {
    *** Companion;
}
-keepclasseswithmembers class bookdoe.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class bookdoe.**$$serializer { *; }
# OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
