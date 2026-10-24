#pragma once
#include <cstdint>
#include <string>

namespace visiondemo {
struct Utf8Segment {
    std::string text;
    bool visible = false;
};
inline bool visibleScalar(uint32_t cp) {
    if (cp <= 0x20 || (cp >= 0x7F && cp <= 0xA0) || cp == 0x1680 ||
        (cp >= 0x2000 && cp <= 0x200F) || (cp >= 0x2028 && cp <= 0x202F) ||
        cp == 0x205F || cp == 0x2060 || cp == 0x3000 || cp == 0xFEFF) return false;
    return true;
}
// Input/output are standard UTF-8, not JNI modified UTF-8. Incomplete scalars remain
// buffered until another token arrives; malformed input is replaced by U+FFFD.
inline Utf8Segment drainUtf8(std::string& pending, bool final) {
    Utf8Segment result;
    size_t i = 0;
    while (i < pending.size()) {
        auto a = static_cast<unsigned char>(pending[i]);
        size_t n = a < 0x80 ? 1 : a >= 0xC2 && a <= 0xDF ? 2 :
            a >= 0xE0 && a <= 0xEF ? 3 : a >= 0xF0 && a <= 0xF4 ? 4 : 0;
        if (n && i + n > pending.size() && !final) break;
        uint32_t cp = n == 1 ? a : n ? a & ((1u << (7-n))-1) : 0;
        bool valid = n && i + n <= pending.size();
        if (valid) for (size_t j = 1; j < n; ++j) {
            auto b = static_cast<unsigned char>(pending[i+j]);
            if ((b & 0xC0) != 0x80) { valid = false; break; }
            cp = (cp << 6) | (b & 63);
        }
        if (valid && ((n == 2 && cp < 0x80) || (n == 3 && cp < 0x800) ||
            (n == 4 && cp < 0x10000) || (cp >= 0xD800 && cp <= 0xDFFF) || cp > 0x10FFFF)) valid = false;
        if (valid) { result.text.append(pending, i, n); i += n; }
        else { result.text += "\xEF\xBF\xBD"; ++i; cp = 0xFFFD; }
        result.visible = result.visible || visibleScalar(cp);
    }
    pending.erase(0, i);
    return result;
}
} // namespace visiondemo
