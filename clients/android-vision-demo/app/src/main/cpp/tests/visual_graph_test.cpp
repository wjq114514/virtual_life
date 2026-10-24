// Standalone host check; unused JNI/MNN functions are removed with --gc-sections.
#include "../mnn_vision_jni.cpp"
#include <cassert>
#include <filesystem>
#include <iostream>

static void writeGraph(const std::string& path, bool external) {
    MNN::NetT net;
    auto op = std::make_unique<MNN::OpT>();
    op->type = MNN::OpType_Const;
    MNN::BlobT blob;
    blob.dims = {4};
    if (external) blob.external = {8, 16};
    else blob.float32s = {1, 2, 3, 4};
    op->main.Set(std::move(blob));
    net.oplists.emplace_back(std::move(op));
    flatbuffers::FlatBufferBuilder builder;
    auto root = MNN::Net::Pack(builder, &net);
    MNN::FinishNetBuffer(builder, root);
    std::ofstream out(path, std::ios::binary);
    out.write(reinterpret_cast<const char*>(builder.GetBufferPointer()), builder.GetSize());
}
static bool rejected(const std::string& path) {
    try { validateVisualGraph(path); return false; }
    catch (const std::runtime_error&) { return true; }
}
int main() {
    char directory[] = "/tmp/vision-graph-test.XXXXXX";
    assert(mkdtemp(directory));
    const std::string path = std::string(directory) + "/visual.mnn";
    assert(rejected(path));
    writeGraph(path, false);
    validateVisualGraph(path); // Inline graph needs no sidecar.
    writeGraph(path, true);
    assert(rejected(path)); // Missing weights.
    { std::ofstream out(path + ".weight", std::ios::binary); out << "short"; }
    assert(rejected(path)); // Truncated weights.
    { std::ofstream out(path + ".weight", std::ios::binary); out << std::string(24, '\0'); }
    validateVisualGraph(path);
    { std::ofstream out(path, std::ios::binary); out << "invalid flatbuffer"; }
    assert(rejected(path));
    std::filesystem::remove_all(directory);
    std::cout << "Visual graph inline/external/missing/truncated/corrupt checks passed\n";
}
