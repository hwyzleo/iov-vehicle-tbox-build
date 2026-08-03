#include "greet.hpp"

#include <sstream>

namespace tbox {

std::string greet(const std::string &name) {
    std::ostringstream oss;
    oss << "TBOX-Hello, " << name << "!";
    return oss.str();
}

}  // namespace tbox
