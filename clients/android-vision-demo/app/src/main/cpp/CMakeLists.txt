cmake_minimum_required(VERSION 3.22.1)
project(vision_mnn LANGUAGES C CXX)
if(NOT ANDROID_ABI STREQUAL "arm64-v8a")
    message(FATAL_ERROR "The vision demo supports arm64-v8a only")
endif()
get_filename_component(DEMO_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../../.." ABSOLUTE)
set(MNN_SOURCE "${DEMO_ROOT}/.deps/MNN")
if(NOT EXISTS "${MNN_SOURCE}/.vision-demo-source")
    message(FATAL_ERROR "Run scripts/prepare-mnn.sh from the repository root first")
endif()
file(READ "${MNN_SOURCE}/.vision-demo-source" MNN_SOURCE_STAMP)
string(STRIP "${MNN_SOURCE_STAMP}" MNN_SOURCE_STAMP)
if(NOT MNN_SOURCE_STAMP STREQUAL "3.6.1 4b6065c4e2674318f5bf1dc75836ce4d30c17bfe598c4a1b11b7d0b2092b06e6")
    message(FATAL_ERROR "Expected the pinned MNN 3.6.1 source archive")
endif()
# Apply to every shared library, including MNN itself (NDK r27+ also accepts the Gradle flag).
set(CMAKE_SHARED_LINKER_FLAGS "${CMAKE_SHARED_LINKER_FLAGS} -Wl,-z,max-page-size=16384 -Wl,-z,common-page-size=16384")
set(MNN_BUILD_SHARED_LIBS ON CACHE BOOL "" FORCE)
set(MNN_SEP_BUILD OFF CACHE BOOL "" FORCE)
# Upstream's Android header-copy POST_BUILD hooks cannot target OBJECT libraries.
# This option skips those packaging hooks; the JNI target includes source headers directly.
set(MNN_BUILD_FOR_ANDROID_COMMAND ON CACHE BOOL "" FORCE)
set(MNN_BUILD_LLM ON CACHE BOOL "" FORCE)
set(MNN_LOW_MEMORY ON CACHE BOOL "" FORCE)
set(MNN_SUPPORT_TRANSFORMER_FUSE ON CACHE BOOL "" FORCE)
set(MNN_ARM82 ON CACHE BOOL "" FORCE)
set(MNN_USE_LOGCAT ON CACHE BOOL "" FORCE)
# MNN_CPU_WEIGHT_DEQUANT_GEMM was removed in 3.6.1; LOW_MEMORY is its replacement.
# OMNI would additionally force the unnecessary audio stack on. OpenCV enables vision itself.
set(MNN_BUILD_LLM_OMNI OFF CACHE BOOL "" FORCE)
set(MNN_BUILD_OPENCV ON CACHE BOOL "" FORCE)
set(MNN_IMGCODECS ON CACHE BOOL "" FORCE)
option(MNN_OPENCL "Compile the optional OpenCL backend" ON)
foreach(flag MNN_BUILD_TEST MNN_BUILD_DEMO MNN_BUILD_TOOLS MNN_LLM_BUILD_DEMO
        MNN_LLM_BUILD_TEST BUILD_MLS MNN_BUILD_AUDIO MNN_BUILD_DIFFUSION
        MNN_BUILD_BENCHMARK MNN_BUILD_QUANTOOLS MNN_BUILD_CONVERTER LLM_SUPPORT_HTTP_RESOURCE)
    set(${flag} OFF CACHE BOOL "" FORCE)
endforeach()
add_subdirectory("${MNN_SOURCE}" "${CMAKE_CURRENT_BINARY_DIR}/mnn" EXCLUDE_FROM_ALL)
# Upstream creates this demo unconditionally; never include it in the Android build.
if(TARGET apply_template)
    set_target_properties(apply_template PROPERTIES EXCLUDE_FROM_ALL TRUE)
endif()
add_library(vision_mnn SHARED mnn_vision_jni.cpp)
target_compile_features(vision_mnn PRIVATE cxx_std_17)
target_compile_options(vision_mnn PRIVATE -fexceptions -frtti -Wall -Wextra)
target_include_directories(vision_mnn PRIVATE "${MNN_SOURCE}/include"
    "${MNN_SOURCE}/transformers/llm/engine/include" "${MNN_SOURCE}/tools/cv/include"
    "${MNN_SOURCE}/schema/current" "${MNN_SOURCE}/3rd_party/flatbuffers/include")
target_link_libraries(vision_mnn PRIVATE MNN log)
if(MNN_OPENCL)
    target_compile_definitions(vision_mnn PRIVATE VISION_DEMO_OPENCL=1)
endif()
