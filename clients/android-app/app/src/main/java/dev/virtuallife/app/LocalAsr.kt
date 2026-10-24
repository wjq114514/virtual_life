package dev.virtuallife.app

import com.k2fsa.sherpa.onnx.EndpointConfig
import com.k2fsa.sherpa.onnx.EndpointRule
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineRecognizer
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig
import java.io.File

/** Thin, single-stream wrapper over sherpa-onnx. Loads weights from the filesystem. */
class LocalAsr private constructor(private val recognizer: OnlineRecognizer) {
    private val stream = recognizer.createStream()

    fun accept(samples: FloatArray, sampleRate: Int) = stream.acceptWaveform(samples, sampleRate)

    fun decodeWhileReady() {
        while (recognizer.isReady(stream)) recognizer.decode(stream)
    }

    fun isEndpoint(): Boolean = recognizer.isEndpoint(stream)

    fun text(): String = recognizer.getResult(stream).text

    fun reset() = recognizer.reset(stream)

    fun release() {
        stream.release()
        recognizer.release()
    }

    companion object {
        const val SAMPLE_RATE = 16_000

        fun load(dir: File): LocalAsr {
            val config = OnlineRecognizerConfig(
                featConfig = FeatureConfig(sampleRate = SAMPLE_RATE, featureDim = 80),
                modelConfig = OnlineModelConfig(
                    transducer = OnlineTransducerModelConfig(
                        encoder = File(dir, "encoder-epoch-99-avg-1.onnx").absolutePath,
                        decoder = File(dir, "decoder-epoch-99-avg-1.onnx").absolutePath,
                        joiner = File(dir, "joiner-epoch-99-avg-1.int8.onnx").absolutePath,
                    ),
                    tokens = File(dir, "tokens.txt").absolutePath,
                    numThreads = 2,
                    modelType = "zipformer",
                ),
                endpointConfig = EndpointConfig(
                    rule1 = EndpointRule(
                        mustContainNonSilence = false,
                        minTrailingSilence = 1.2f,
                        minUtteranceLength = 0.0f,
                    ),
                    rule2 = EndpointRule(
                        mustContainNonSilence = true,
                        minTrailingSilence = 0.8f,
                        minUtteranceLength = 0.0f,
                    ),
                    rule3 = EndpointRule(
                        mustContainNonSilence = false,
                        minTrailingSilence = 2.4f,
                        minUtteranceLength = 0.0f,
                    ),
                ),
                enableEndpoint = true,
                decodingMethod = "greedy_search",
                maxActivePaths = 4,
            )
            return LocalAsr(OnlineRecognizer(config = config))
        }
    }
}
