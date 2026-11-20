// FPP channel output: forwards the configured channel range to the local
// bulb bridge daemon (127.0.0.1:4051) as DDP packets. Only changed 1440-byte
// chunks are sent, plus a full refresh every 30 s. Never blocks the frame loop.
#include "Plugin.h"
#include "ChannelOutput.h"

#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
constexpr size_t kChunk = 1440;                       // multiple of 3, one DDP packet
constexpr auto kRefresh = std::chrono::seconds(30);
#ifndef SMARTBULB_PORT
#define SMARTBULB_PORT 4051
#endif

class SmartBulbOutput final : public ChannelOutput {
public:
    SmartBulbOutput(unsigned int start, unsigned int count)
        : ChannelOutput(start, count), prev_(count, 0) {
        addr_.sin_family = AF_INET;
        addr_.sin_port = htons(SMARTBULB_PORT);
        inet_pton(AF_INET, "127.0.0.1", &addr_.sin_addr);
        sock_ = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
    }
    ~SmartBulbOutput() override { if (sock_ >= 0) close(sock_); }

    std::string GetOutputType() const override { return "SmartBulbBridge"; }

    void GetRequiredChannelRanges(const std::function<void(int, int)>& addRange) override {
        addRange(m_startChannel, m_startChannel + m_channelCount - 1);
    }

    int SendData(unsigned char* data) override {
        if (sock_ < 0 || !data) return 0;
        const auto now = Clock::now();
        const bool full = !primed_ || now - lastFull_ >= kRefresh;
        for (size_t off = 0; off < m_channelCount; off += kChunk) {
            const size_t len = std::min<size_t>(kChunk, m_channelCount - off);
            if (!full && std::memcmp(prev_.data() + off, data + off, len) == 0) continue;
            unsigned char pkt[10 + kChunk];
            const uint32_t abs = static_cast<uint32_t>(m_startChannel + off);  // 0-based
            pkt[0] = 0x41; pkt[1] = 0; pkt[2] = 1; pkt[3] = 1;
            pkt[4] = abs >> 24; pkt[5] = abs >> 16; pkt[6] = abs >> 8; pkt[7] = abs;
            pkt[8] = len >> 8;  pkt[9] = len;
            std::memcpy(pkt + 10, data + off, len);
            sendto(sock_, pkt, 10 + len, MSG_DONTWAIT,
                   reinterpret_cast<const sockaddr*>(&addr_), sizeof(addr_));
            std::memcpy(prev_.data() + off, data + off, len);
        }
        if (full) { primed_ = true; lastFull_ = now; }
        return 0;
    }

private:
    int sock_ = -1;
    sockaddr_in addr_{};
    std::vector<unsigned char> prev_;
    bool primed_ = false;
    Clock::time_point lastFull_{};
};

class SmartBulbPlugin final : public FPPPlugin {
public:
    SmartBulbPlugin() : FPPPlugin("SmartBulbBridge") {}
    ChannelOutput* createChannelOutput(unsigned int start, unsigned int count) override {
        if (count < 3 || count % 3 != 0 || count > 65535) return nullptr;
        return new SmartBulbOutput(start, count);
    }
};
}  // namespace

extern "C" FPPPlugins::Plugin* createPlugin() { return new SmartBulbPlugin(); }
