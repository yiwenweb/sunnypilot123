#pragma once

#include <cassert>
#include <memory>
#include <string>

#include "cereal/messaging/messaging.h"
#include "common/util.h"
#include "system/hardware/hw.h"

#include <bzlib.h>

class RawFile {
 public:
  RawFile(const std::string &path, bool use_bz2 = false) {
    std::string file_path = path + (use_bz2? ".bz2" : "");
    file = util::safe_fopen(file_path.c_str(), "wb");
    assert(file != nullptr);
    if (use_bz2) {
      int bzerror;
      bz_file = BZ2_bzWriteOpen(&bzerror, file, 9, 0, 30);
      assert(bzerror == BZ_OK);
    }
  }
  ~RawFile() {
    if (bz_file) {
      int bzerror;
      BZ2_bzWriteClose(&bzerror, bz_file, 0, nullptr, nullptr);
      assert(bzerror == BZ_OK);
    } else {
      util::safe_fflush(file);
      int err = fclose(file);
      assert(err == 0);
    }
  }
  inline void write(void* data, size_t size) {
    if (bz_file) {
      size_t written = 0;
      int bzerror;

      size_t count = 1;
      while (written < size) {
        BZ2_bzWrite(&bzerror, bz_file, (void*)((char*)data + written * count), count * (size - written));
        if (bzerror == BZ_IO_ERROR && errno == EINTR) {
          continue; // Retry if interrupted
        } else if (bzerror != BZ_OK) {
          break; // Exit on other errors
        }
        written = size; // All data written successfully
      }

      assert(written == size);
    } else {
      size_t written = util::safe_fwrite(data, 1, size, file);
      assert(written == size);
    }
  }
  inline void write(kj::ArrayPtr<capnp::byte> array) { write(array.begin(), array.size()); }

 private:
  FILE* file = nullptr;
  BZFILE* bz_file = nullptr;
};

typedef cereal::Sentinel::SentinelType SentinelType;


class LoggerState {
public:
  LoggerState(const std::string& log_root = Path::log_root());
  ~LoggerState();
  bool next();
  void write(uint8_t* data, size_t size, bool in_qlog);
  inline int segment() const { return part; }
  inline const std::string& segmentPath() const { return segment_path; }
  inline const std::string& routeName() const { return route_name; }
  inline void write(kj::ArrayPtr<kj::byte> bytes, bool in_qlog) { write(bytes.begin(), bytes.size(), in_qlog); }
  inline void setExitSignal(int signal) { exit_signal = signal; }

protected:
  int part = -1, exit_signal = 0;
  std::string route_path, route_name, segment_path, lock_file;
  kj::Array<capnp::word> init_data;
  std::unique_ptr<RawFile> rlog, qlog;
};

kj::Array<capnp::word> logger_build_init_data();
kj::Array<capnp::word> logger_build_params_data_car_start();
std::string logger_get_identifier(std::string key);
