# UniFFI/JNA use reflection and JNI names; do not rename their native boundary.
-keep class computer.iroh.** { *; }
-keep class uniffi.** { *; }
-keep class com.sun.jna.** { *; }
-dontwarn com.sun.jna.**
-dontwarn java.awt.**
-keepclasseswithmembernames,includedescriptorclasses class * { native <methods>; }
