// Export every internal function as an individual decompiler file plus TSV index.
// Usage: -postScript ExportAllDecomp.java /absolute/output/directory

import java.io.File;
import java.io.PrintWriter;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;

public class ExportAllDecomp extends GhidraScript {
    private static String clean(String value) {
        return value.replaceAll("[^A-Za-z0-9._-]", "_");
    }

    private static String tsv(String value) {
        return value == null ? "" : value.replace("\\", "\\\\")
            .replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n");
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 1) {
            throw new IllegalArgumentException("expected absolute output directory");
        }
        File root = new File(args[0]);
        File functionsDir = new File(root, "functions");
        if (!functionsDir.mkdirs() && !functionsDir.isDirectory()) {
            throw new IllegalStateException("cannot create " + functionsDir);
        }

        DecompInterface decompiler = new DecompInterface();
        decompiler.toggleCCode(true);
        decompiler.toggleSyntaxTree(true);
        if (!decompiler.openProgram(currentProgram)) {
            throw new IllegalStateException("cannot open program in decompiler");
        }

        int total = 0;
        int success = 0;
        int failed = 0;
        try (PrintWriter index = new PrintWriter(new File(root, "functions.tsv"), "UTF-8")) {
            index.println("entry\tname\tinternal\tthunk\tbody_addresses\tstatus\terror\tfile");
            FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
            while (functions.hasNext() && !monitor.isCancelled()) {
                Function function = functions.next();
                total++;
                String entry = function.getEntryPoint().toString();
                String filename = entry + "__" + clean(function.getName()) + ".c";
                File output = new File(functionsDir, filename);
                String status = "external";
                String error = "";

                try (PrintWriter writer = new PrintWriter(output, "UTF-8")) {
                    writer.println("/* entry: " + entry + " */");
                    writer.println("/* name: " + function.getName() + " */");
                    writer.println("/* body: " + function.getBody() + " */");
                    if (function.isExternal()) {
                        writer.println("/* external function: no body */");
                    }
                    else {
                        DecompileResults result = decompiler.decompileFunction(function, 120, monitor);
                        if (result.decompileCompleted() && result.getDecompiledFunction() != null) {
                            status = "ok";
                            success++;
                            writer.print(result.getDecompiledFunction().getC());
                        }
                        else {
                            status = "failed";
                            failed++;
                            error = result.getErrorMessage();
                            writer.println("/* DECOMPILE FAILED: " + tsv(error) + " */");
                        }
                    }
                }

                index.println(entry + "\t" + tsv(function.getName()) + "\t" +
                    (!function.isExternal()) + "\t" + function.isThunk() + "\t" +
                    tsv(function.getBody().toString()) + "\t" + status + "\t" +
                    tsv(error) + "\tfunctions/" + filename);
                if (total % 250 == 0) {
                    println("EXPORT_PROGRESS total=" + total + " ok=" + success + " failed=" + failed);
                }
            }
        }
        finally {
            decompiler.dispose();
        }
        try (PrintWriter summary = new PrintWriter(new File(root, "summary.txt"), "UTF-8")) {
            summary.println("program=" + currentProgram.getName());
            summary.println("executable_md5=" + currentProgram.getExecutableMD5());
            summary.println("total=" + total);
            summary.println("success=" + success);
            summary.println("failed=" + failed);
        }
        println("EXPORT_DONE total=" + total + " ok=" + success + " failed=" + failed);
    }
}
