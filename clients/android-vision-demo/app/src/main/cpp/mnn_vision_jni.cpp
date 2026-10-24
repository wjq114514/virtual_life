#include <jni.h>
#include "utf8_segments.hpp"
#include <llm/llm.hpp>
#include <cv/imgcodecs.hpp>
#include <atomic>
#include <chrono>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <streambuf>
#include <unordered_map>

using MNN::Transformer::Llm;
using MNN::Transformer::LlmStatus;
using Clock = std::chrono::steady_clock;
namespace {
struct Session {
    std::unique_ptr<Llm, decltype(&Llm::destroy)> llm{nullptr, Llm::destroy};
    std::atomic<bool> cancelled{false};
    std::mutex operation;
};
std::mutex registryMutex;
std::unordered_map<jlong, std::shared_ptr<Session>> sessions;
jlong nextId = 1;
std::shared_ptr<Session> find(jlong id) {
    std::lock_guard<std::mutex> lock(registryMutex);
    auto it = sessions.find(id);
    if (it == sessions.end()) throw std::runtime_error("Model handle is no longer loaded");
    return it->second;
}
void fail(JNIEnv* env, const char* message, const char* type = "java/lang/IllegalStateException") {
    if (env->ExceptionCheck()) return;
    jclass cls = env->FindClass(type);
    if (cls) { env->ThrowNew(cls, message); env->DeleteLocalRef(cls); }
}
// JNI modified UTF-8 is not standard UTF-8: explicitly encode UTF-16, including emoji.
std::string utf8(JNIEnv* env, jstring input) {
    if (!input) throw std::runtime_error("Null input string");
    const jchar* chars = env->GetStringChars(input, nullptr);
    if (!chars) throw std::runtime_error("Cannot access input string");
    const jsize length = env->GetStringLength(input);
    std::string out;
    try {
        for (jsize i = 0; i < length; ++i) {
            uint32_t c = chars[i];
            if (c >= 0xD800 && c <= 0xDBFF && i + 1 < length &&
                chars[i+1] >= 0xDC00 && chars[i+1] <= 0xDFFF) {
                c = 0x10000 + ((c - 0xD800) << 10) + (chars[++i] - 0xDC00);
            } else if (c >= 0xD800 && c <= 0xDFFF) c = 0xFFFD;
            if (c < 0x80) out += static_cast<char>(c);
            else if (c < 0x800) {
                out += static_cast<char>(0xC0 | (c >> 6));
                out += static_cast<char>(0x80 | (c & 63));
            } else if (c < 0x10000) {
                out += static_cast<char>(0xE0 | (c >> 12));
                out += static_cast<char>(0x80 | ((c >> 6) & 63));
                out += static_cast<char>(0x80 | (c & 63));
            } else {
                out += static_cast<char>(0xF0 | (c >> 18));
                out += static_cast<char>(0x80 | ((c >> 12) & 63));
                out += static_cast<char>(0x80 | ((c >> 6) & 63));
                out += static_cast<char>(0x80 | (c & 63));
            }
        }
    } catch (...) { env->ReleaseStringChars(input, chars); throw; }
    env->ReleaseStringChars(input, chars);
    return out;
}

// MNN may flush one token halfway through a Unicode scalar. Buffer the incomplete suffix.
class DeltaStream final : public std::streambuf {
public:
    DeltaStream(JNIEnv* env, jobject callback, Session& session, Clock::time_point start)
        : env(env), callback(callback), session(session), start(start) {
        auto cls = env->GetObjectClass(callback);
        if (!cls) throw std::runtime_error("Cannot access stream callback");
        method = env->GetMethodID(cls, "onDelta", "([B)V");
        if (method) cancelMethod = env->GetMethodID(cls, "isCancelled", "()Z");
        env->DeleteLocalRef(cls);
        if (!method || !cancelMethod) throw std::runtime_error("Missing stream or cancellation callback");
    }
    ~DeltaStream() override { if (error) env->DeleteLocalRef(error); }
    double firstMs = -1;
    jthrowable error = nullptr;
    void finish() { emit(true); }
    void rethrowCallback() { if (error) env->Throw(error); }
    bool isCancelled() {
        if (error || session.cancelled.load()) return true;
        const auto cancelled = env->CallBooleanMethod(callback, cancelMethod);
        captureException();
        if (cancelled == JNI_TRUE) session.cancelled.store(true);
        return error || session.cancelled.load();
    }
protected:
    std::streamsize xsputn(const char* s, std::streamsize n) override {
        if (!error && !session.cancelled.load()) { pending.append(s, static_cast<size_t>(n)); emit(false); }
        return n;
    }
    int_type overflow(int_type ch) override {
        if (!traits_type::eq_int_type(ch, traits_type::eof())) {
            char c = traits_type::to_char_type(ch); xsputn(&c, 1);
        }
        return traits_type::not_eof(ch);
    }
    int sync() override { emit(false); return 0; }
private:
    JNIEnv* env;
    jobject callback;
    jmethodID method = nullptr;
    jmethodID cancelMethod = nullptr;
    Session& session;
    Clock::time_point start;
    std::string pending;
    void captureException() {
        if (env->ExceptionCheck()) {
            error = env->ExceptionOccurred();
            env->ExceptionClear();
            session.cancelled.store(true);
        }
    }
    void emit(bool final) {
        if (isCancelled()) return;
        auto segment = visiondemo::drainUtf8(pending, final);
        const auto& output = segment.text;
        const bool visible = segment.visible;
        if (output.empty()) return;
        auto bytes = env->NewByteArray(static_cast<jsize>(output.size()));
        if (!bytes) { captureException(); return; }
        env->SetByteArrayRegion(bytes, 0, static_cast<jsize>(output.size()),
            reinterpret_cast<const jbyte*>(output.data()));
        if (!env->ExceptionCheck()) {
            if (visible && firstMs < 0) firstMs = std::chrono::duration<double, std::milli>(Clock::now()-start).count();
            env->CallVoidMethod(callback, method, bytes);
        }
        env->DeleteLocalRef(bytes);
        captureException();
    }
};
// Unbind the stack ostream even when callback, cancellation or inference fails.
struct ResetContext {
    Llm* llm;
    ~ResetContext() noexcept {
        try { llm->generate_init(nullptr, ""); llm->reset(); } catch (...) {}
    }
};
}

extern "C" JNIEXPORT jlong JNICALL
Java_dev_virtuallife_visiondemo_inference_MnnVisionEngine_nativeLoad(
        JNIEnv* env, jobject, jstring config, jstring backend, jint threads) {
    try {
        auto mode = utf8(env, backend);
        if ((mode != "cpu" && mode != "opencl") || threads < 1 || threads > 64)
            throw std::runtime_error("Invalid model backend or thread count");
#ifndef VISION_DEMO_OPENCL
        if (mode == "opencl") throw std::runtime_error("OpenCL was disabled at build time");
#endif
        auto session = std::make_shared<Session>();
        session->llm.reset(Llm::createLLM(utf8(env, config)));
        if (!session->llm) throw std::runtime_error("Cannot create MNN-LLM");
        // Synchronous vision gives separate, completed vision/prefill timings. Explicitly
        // disable history, prefix cache and speculative decoding for independent images.
        std::string settings = "{\"backend_type\":\"" + mode + "\",\"thread_num\":" +
            std::to_string(threads) + ",\"mllm\":{\"backend_type\":\"" + mode +
            "\",\"thread_num\":" + std::to_string(threads) +
            "},\"async\":false,\"reuse_kv\":false,\"prompt_cache\":false,\"speculative_type\":\"\"}";
        if (!session->llm->set_config(settings) || !session->llm->load())
            throw std::runtime_error("MNN visual model loading failed; check model assets and backend");
        std::lock_guard<std::mutex> lock(registryMutex);
        const jlong id = nextId++;
        sessions.emplace(id, std::move(session));
        return id;
    } catch (const std::exception& error) { fail(env, error.what()); }
      catch (...) { fail(env, "Unknown native model loading failure"); }
    return 0;
}

extern "C" JNIEXPORT jdoubleArray JNICALL
Java_dev_virtuallife_visiondemo_inference_MnnVisionEngine_nativeAnalyze(
        JNIEnv* env, jobject, jlong id, jstring image, jstring prompt, jint maxTokens, jobject callback) {
    try {
        auto session = find(id);
        std::unique_lock<std::mutex> lock(session->operation, std::try_to_lock);
        if (!lock.owns_lock()) throw std::runtime_error("Inference is already active");
        if (maxTokens < 1 || maxTokens > 8192 || !callback) throw std::runtime_error("Invalid inference arguments");
        session->cancelled.store(false);
        const auto start = Clock::now();
        auto* llm = session->llm.get();
        llm->reset();
        DeltaStream buffer(env, callback, *session, start);
        std::ostream output(&buffer);
        ResetContext reset{llm};
        auto checkCancellation = [&]() {
            if (!buffer.isCancelled()) return false;
            if (buffer.error) buffer.rethrowCallback();
            else fail(env, "Inference cancelled", "java/util/concurrent/CancellationException");
            return true;
        };
        // stop() may have happened before this call cleared the per-run atomic flag.
        // The persistent controller predicate must be checked at the native boundary.
        if (checkCancellation()) return nullptr;
        const auto imagePath = utf8(env, image);
        if (imagePath.find_first_of("<>\r\n") != std::string::npos)
            throw std::runtime_error("Image path contains unsupported media delimiter characters");
        // Validate decoding before MNN's multimodal path (which otherwise silently skips
        // an unreadable image). Image variables used for inference are constructed inside
        // the official response() ExecutorScope, not on the thread's default executor.
        {
            if (checkCancellation()) return nullptr;
            auto checkImage = MNN::CV::imread(imagePath);
            if (checkImage.get() == nullptr || !checkImage->getInfo() || checkImage->getInfo()->size == 0)
                throw std::runtime_error("Cannot decode input image");
        }
        auto userPrompt = utf8(env, prompt);
        if (userPrompt.find("<img>") != std::string::npos || userPrompt.find("<audio>") != std::string::npos)
            throw std::runtime_error("Prompt contains media tags");
        MNN::Transformer::MultimodalPrompt media;
        media.prompt_template = "<img>" + imagePath + "</img>\n" + userPrompt;
        if (checkCancellation()) return nullptr;
        llm->response(media, &output, "", 0);
        if (checkCancellation()) return nullptr;
        if (llm->getContext()->vision_us <= 0)
            throw std::runtime_error("Model did not encode the image as visual input");
        for (int token = 0; token < maxTokens; ++token) {
            if (checkCancellation()) return nullptr;
            auto status = llm->getContext()->status;
            if (status == LlmStatus::INTERNAL_ERROR || status == LlmStatus::TIMEOUT ||
                status == LlmStatus::NOT_LOADED || llm->stoped()) break;
            llm->generate(1);
        }
        if (checkCancellation()) return nullptr;
        buffer.finish();
        if (checkCancellation()) return nullptr;
        if (!output.good()) throw std::runtime_error("Native output stream failed");
        auto* stats = llm->getContext();
        if (stats->status == LlmStatus::INTERNAL_ERROR || stats->status == LlmStatus::TIMEOUT ||
            stats->status == LlmStatus::NOT_LOADED)
            throw std::runtime_error("MNN inference failed or timed out");
        const jdouble values[] = {buffer.firstMs,
            std::chrono::duration<double, std::milli>(Clock::now()-start).count(),
            stats->vision_us/1000.0, stats->prefill_us/1000.0, stats->decode_us/1000.0};
        auto result = env->NewDoubleArray(5);
        if (result) env->SetDoubleArrayRegion(result, 0, 5, values);
        return result;
    } catch (const std::exception& error) { fail(env, error.what()); }
      catch (...) { fail(env, "Unknown native inference failure"); }
    return nullptr;
}

extern "C" JNIEXPORT void JNICALL
Java_dev_virtuallife_visiondemo_inference_MnnVisionEngine_nativeCancel(JNIEnv*, jobject, jlong id) {
    std::lock_guard<std::mutex> lock(registryMutex);
    auto it = sessions.find(id);
    if (it != sessions.end()) it->second->cancelled.store(true);
}
extern "C" JNIEXPORT void JNICALL
Java_dev_virtuallife_visiondemo_inference_MnnVisionEngine_nativeDestroy(JNIEnv*, jobject, jlong id) {
    std::shared_ptr<Session> released;
    { // Never hold the registry mutex during a potentially expensive MNN destructor.
        std::lock_guard<std::mutex> lock(registryMutex);
        auto it = sessions.find(id);
        if (it == sessions.end()) return;
        released = std::move(it->second);
        sessions.erase(it);
    }
    released->cancelled.store(true);
    // Active nativeAnalyze retains shared ownership until all MNN calls have returned.
}

// Validate the pinned schema before MNN touches a missing or truncated visual weight
// sidecar. mmap keeps this check bounded in resident memory for large visual graphs.
#include <MNN_generated.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
namespace {
struct MappedGraph {
    int fd = -1;
    void* data = MAP_FAILED;
    size_t size = 0;
    ~MappedGraph() {
        if (data != MAP_FAILED) munmap(data, size);
        if (fd >= 0) ::close(fd);
    }
};
void validateVisualGraph(const std::string& path) {
    MappedGraph graph;
    graph.fd = open(path.c_str(), O_RDONLY | O_CLOEXEC);
    struct stat info{};
    if (graph.fd < 0 || fstat(graph.fd, &info) != 0 || info.st_size <= 0 ||
        info.st_size > 1024LL * 1024 * 1024)
        throw std::runtime_error("Visual graph is missing, empty or too large (limit 1 GiB)");
    graph.size = static_cast<size_t>(info.st_size);
    graph.data = mmap(nullptr, graph.size, PROT_READ, MAP_PRIVATE, graph.fd, 0);
    if (graph.data == MAP_FAILED) throw std::runtime_error("Cannot read visual graph");
    auto bytes = static_cast<const uint8_t*>(graph.data);
    flatbuffers::Verifier verifier(bytes, graph.size);
    if (!MNN::VerifyNetBuffer(verifier)) throw std::runtime_error("Invalid visual MNN graph");
    auto net = MNN::GetNet(bytes);
    auto inspect = [&](const MNN::Op* op) {
        const flatbuffers::Vector<int64_t>* external = nullptr;
        if (auto param = op->main_as_Blob()) external = param->external();
        if (auto param = op->main_as_Convolution2D()) external = param->external();
        if (auto param = op->main_as_Convolution3D()) external = param->external();
        if (auto param = op->main_as_Scale()) external = param->external();
        if (auto param = op->main_as_LayerNorm()) external = param->external();
        if (auto param = op->main_as_GroupNorm()) external = param->external();
        if (!external || external->size() < 2) return;
        int64_t end = external->Get(0);
        if (end < 0) throw std::runtime_error("Invalid visual external weight offset");
        for (flatbuffers::uoffset_t i = 1; i < external->size(); ++i) {
            const int64_t length = external->Get(i);
            if (length < 0 || end > INT64_MAX - length)
                throw std::runtime_error("Invalid visual external weight range");
            end += length;
        }
        if (end == external->Get(0)) return;
        // Module::load(file) sets the default external file to file + '.weight'.
        const std::string sidecar = op->externalPath() ? op->externalPath()->str() : path + ".weight";
        struct stat weights{};
        if (stat(sidecar.c_str(), &weights) != 0 || !S_ISREG(weights.st_mode) ||
            access(sidecar.c_str(), R_OK) != 0 || weights.st_size < end)
            throw std::runtime_error("Visual external weights missing, unreadable or truncated (.mnn.weight)");
    };
    if (net->oplists()) for (auto op : *net->oplists()) inspect(op);
    if (net->subgraphs()) for (auto subgraph : *net->subgraphs())
        if (subgraph->nodes()) for (auto op : *subgraph->nodes()) inspect(op);
}
}
extern "C" JNIEXPORT void JNICALL
Java_dev_virtuallife_visiondemo_inference_MnnVisionEngine_nativeValidateVisualGraph(
        JNIEnv* env, jobject, jstring path) {
    try { validateVisualGraph(utf8(env, path)); }
    catch (const std::exception& error) { fail(env, error.what()); }
    catch (...) { fail(env, "Visual graph validation failed"); }
}
