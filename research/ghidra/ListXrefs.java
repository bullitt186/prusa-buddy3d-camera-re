// List references to one or more addresses, including the containing function.
// Usage: -postScript ListXrefs.java 0x3e295f 0x3ee07f

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

public class ListXrefs extends GhidraScript {
    @Override
    protected void run() throws Exception {
        for (String value : getScriptArgs()) {
            Address target = toAddr(Long.decode(value));
            println("===== XREFS " + target + " =====");
            ReferenceIterator refs = currentProgram.getReferenceManager().getReferencesTo(target);
            while (refs.hasNext()) {
                Reference ref = refs.next();
                Address from = ref.getFromAddress();
                Function function = currentProgram.getFunctionManager().getFunctionContaining(from);
                println(from + " " + ref.getReferenceType() + " " +
                    (function == null ? "<no-function>" :
                        function.getName() + "@" + function.getEntryPoint()));
            }
        }
    }
}
