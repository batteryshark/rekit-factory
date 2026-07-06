// DecompileToC.java — a GhidraScript that decompiles every function in the
// analyzed program to C and appends it to a single output file.
//
// Driven by the ghidra-decompile transform via headless:
//   analyzeHeadless <proj> rekit -import <bin> \
//       -scriptPath <dir> -postScript DecompileToC.java <out.c> -deleteProject
//
// getScriptArgs()[0] is the output path. We use DecompInterface directly (rather
// than the GUI decompiler) because headless has no UI: open it once, decompile
// each function with a per-function timeout, and write the C it returns. A
// failed/timed-out function is noted as a comment rather than aborting the run —
// one hostile or pathological function shouldn't lose the rest of the output.
//@category Analysis

import java.io.PrintWriter;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.util.task.ConsoleTaskMonitor;

public class DecompileToC extends GhidraScript {

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 1) {
            println("DecompileToC: missing output path argument");
            return;
        }
        String outPath = args[0];

        DecompInterface decomp = new DecompInterface();
        decomp.openProgram(currentProgram);
        try (PrintWriter out = new PrintWriter(outPath)) {
            out.println("// Decompiled by Ghidra from " + currentProgram.getName());
            for (Function func : currentProgram.getFunctionManager().getFunctions(true)) {
                if (monitor.isCancelled()) {
                    break;
                }
                // 60s per function bounds pathological/adversarial cases.
                DecompileResults res = decomp.decompileFunction(func, 60, new ConsoleTaskMonitor());
                if (res != null && res.decompileCompleted()) {
                    out.println(res.getDecompiledFunction().getC());
                } else {
                    String err = (res != null) ? res.getErrorMessage() : "no result";
                    out.println("// [decompile failed: " + func.getName() + "] " + err);
                }
            }
        } finally {
            decomp.dispose();
        }
    }
}
