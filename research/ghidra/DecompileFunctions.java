// Decompile one or more function addresses from a headless Ghidra project.
// Usage: -postScript DecompileFunctions.java 0x61bbc 0xb87b4

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class DecompileFunctions extends GhidraScript {
    @Override
    protected void run() throws Exception {
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);

        try {
            for (String value : getScriptArgs()) {
                Address address = toAddr(Long.decode(value));
                Function function = currentProgram.getFunctionManager()
                    .getFunctionContaining(address);
                if (function == null) {
                    println("NO_FUNCTION " + value);
                    continue;
                }

                println("===== " + value + " " + function.getName() + " " +
                    function.getEntryPoint() + "-" + function.getBody().getMaxAddress() +
                    " =====");
                DecompileResults result = decompiler.decompileFunction(
                    function, 120, monitor);
                if (!result.decompileCompleted()) {
                    println("DECOMPILE_FAILED " + result.getErrorMessage());
                    continue;
                }
                println(result.getDecompiledFunction().getC());
            }
        }
        finally {
            decompiler.dispose();
        }
    }
}
