// Host-only JNI callback harness. No model is loaded or inference output simulated.
#include "../mnn_vision_jni.cpp"
#include <cassert>
#include <iostream>

namespace MNN::Transformer {
void Llm::destroy(Llm*) { assert(false && "This callback test must never own a model"); }
}
namespace {
bool controllerCancelled = false;
bool throwPredicate = false;
bool pendingException = false;
int predicateCalls = 0;
int deltaCalls = 0;
jthrowable throwable = reinterpret_cast<jthrowable>(3);
jthrowable rethrown = nullptr;
jclass JNICALL getClass(JNIEnv*, jobject) { return reinterpret_cast<jclass>(1); }
jmethodID JNICALL getMethod(JNIEnv*, jclass, const char*, const char*) {
    return reinterpret_cast<jmethodID>(2);
}
void JNICALL deleteRef(JNIEnv*, jobject) {}
jboolean JNICALL callBoolean(JNIEnv*, jobject, jmethodID, va_list) {
    ++predicateCalls;
    if (throwPredicate) pendingException = true;
    return controllerCancelled ? JNI_TRUE : JNI_FALSE;
}
jboolean JNICALL exceptionCheck(JNIEnv*) { return pendingException ? JNI_TRUE : JNI_FALSE; }
jthrowable JNICALL exceptionOccurred(JNIEnv*) { return throwable; }
void JNICALL exceptionClear(JNIEnv*) { pendingException = false; }
jint JNICALL throwError(JNIEnv*, jthrowable error) { rethrown = error; return 0; }
void JNICALL callVoid(JNIEnv*, jobject, jmethodID, va_list) { ++deltaCalls; }
}
int main() {
    JNINativeInterface_ functions{};
    functions.GetObjectClass = getClass;
    functions.GetMethodID = getMethod;
    functions.DeleteLocalRef = deleteRef;
    functions.CallBooleanMethodV = callBoolean;
    functions.CallVoidMethodV = callVoid;
    functions.ExceptionCheck = exceptionCheck;
    functions.ExceptionOccurred = exceptionOccurred;
    functions.ExceptionClear = exceptionClear;
    functions.Throw = throwError;
    JNIEnv env{&functions};
    const auto callback = reinterpret_cast<jobject>(1);
    Session session;
    {
        // Reproduce stop before nativeAnalyze: clearing the atomic cannot clear the
        // persistent controller predicate, so no image/model work may follow.
        session.cancelled.store(true);
        controllerCancelled = true;
        session.cancelled.store(false);
        DeltaStream stream(&env, callback, session, Clock::now());
        assert(stream.isCancelled());
        assert(session.cancelled.load() && predicateCalls == 1);
        assert(!stream.error);
        std::ostream output(&stream);
        output << "stale output" << std::flush;
        assert(deltaCalls == 0);
    }
    {
        controllerCancelled = false;
        session.cancelled.store(false);
        DeltaStream stream(&env, callback, session, Clock::now());
        assert(!stream.isCancelled());
        // A session can become stale while image processing/prefill is running.
        controllerCancelled = true;
        assert(stream.isCancelled() && session.cancelled.load());
    }
    {
        session.cancelled.store(false);
        controllerCancelled = false;
        throwPredicate = true;
        DeltaStream stream(&env, callback, session, Clock::now());
        assert(stream.isCancelled());
        assert(stream.error == throwable && !pendingException);
        assert(session.cancelled.load());
        stream.rethrowCallback();
        assert(rethrown == throwable); // Preserve original exception identity.
    }
    std::cout << "JNI persistent cancellation, stale-output and predicate-exception checks passed\n";
}
