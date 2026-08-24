# Chaquopy resolves Java classes from Python by name at runtime, so anything
# reachable only that way must survive shrinking. Minification is off in the
# release build for this reason; these rules are here for when it is turned on.
-keep class com.chaquo.python.** { *; }
-keep class com.spine.ps2.** { *; }
-dontwarn com.chaquo.python.**
