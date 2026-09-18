// Export Ghidra Function-ID hashes for relocation-insensitive binary matching.
// Usage: -postScript ExportFidHashes.java /absolute/output.tsv

import java.io.File;
import java.io.PrintWriter;

import ghidra.app.script.GhidraScript;
import ghidra.feature.fid.hash.FidHashQuad;
import ghidra.feature.fid.service.FidService;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;

public class ExportFidHashes extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 1) {
            throw new IllegalArgumentException("expected absolute output TSV path");
        }
        FidService service = new FidService();
        int total = 0;
        int hashed = 0;
        try (PrintWriter out = new PrintWriter(new File(args[0]), "UTF-8")) {
            out.println("entry\tname\tbody_size\tfid_hash");
            FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
            while (functions.hasNext() && !monitor.isCancelled()) {
                Function function = functions.next();
                total++;
                FidHashQuad hash = function.isExternal() ? null : service.hashFunction(function);
                if (hash != null) {
                    hashed++;
                }
                out.println(function.getEntryPoint() + "\t" +
                    function.getName().replace("\t", "\\t") + "\t" +
                    function.getBody().getNumAddresses() + "\t" +
                    (hash == null ? "" : hash.toString()));
            }
        }
        println("FID_EXPORT_DONE total=" + total + " hashed=" + hashed);
    }
}
