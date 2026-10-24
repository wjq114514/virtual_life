#include "../utf8_segments.hpp"
#include <cassert>
#include <iostream>

int main() {
    const std::string original = u8"你好🙂𠀀hello\n世界";
    // Every possible partition: token boundaries can split any Unicode scalar.
    for (size_t a = 0; a <= original.size(); ++a) {
        for (size_t b = a; b <= original.size(); ++b) {
            std::string pending;
            std::string result;
            pending += original.substr(0, a);
            result += visiondemo::drainUtf8(pending, false).text;
            pending += original.substr(a, b-a);
            result += visiondemo::drainUtf8(pending, false).text;
            pending += original.substr(b);
            result += visiondemo::drainUtf8(pending, true).text;
            assert(result == original && pending.empty());
        }
    }
    std::string pending;
    std::string result;
    for (char c : original) {
        pending += c;
        result += visiondemo::drainUtf8(pending, false).text;
    }
    assert(result == original && pending.empty());
    pending = "\xF0\x9F";
    assert(visiondemo::drainUtf8(pending, false).text.empty());
    assert(!visiondemo::drainUtf8(pending, true).text.empty() && pending.empty());
    for (const auto& malformed : {std::string("\xC0\xAF"), std::string("\xED\xA0\x80"),
            std::string("\xF4\x90\x80\x80"), std::string("\x80")}) {
        pending = malformed;
        auto fixed = visiondemo::drainUtf8(pending, true).text;
        assert(fixed != malformed && pending.empty());
    }
    pending = u8" \t\r\n\u00a0\u200b\u3000";
    assert(!visiondemo::drainUtf8(pending, true).visible);
    pending = u8"中";
    assert(visiondemo::drainUtf8(pending, false).visible);
    pending = std::string("a\0b", 3);
    assert(visiondemo::drainUtf8(pending, true).text == std::string("a\0b", 3));
    std::cout << "UTF-8 partition, malformed, whitespace, emoji and NUL checks passed\n";
}
