// TBOX Build - Installed-package consumer (CR-002 §10.4)
//
// Minimal consumer that links the framework SDK via the TBoxFramework::
// component targets. It only includes public framework headers installed into
// the SDK staging; it never references framework source or build trees.
//
// Gated on FW CR-010: the exact public headers / component targets are
// finalised by the framework side CR. This file uses the expected public API
// surface per the BUILD<->FW contract and is compiled once the SDK staging is
// populated.

#include <tbox-framework/config.h>
#include <tbox-framework/store.h>
#include <tbox-framework/log.h>
#include <tbox-framework/ipc.h>

int main() {
    // The consumer proves that the installed SDK staging is self-contained:
    // headers resolve, component targets link, and the resulting binary is a
    // clean AArch64 ELF with only registered NEEDED entries.
    return 0;
}
