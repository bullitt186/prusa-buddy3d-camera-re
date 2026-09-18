// Print data and pointer/string targets at one or more addresses.
// Usage: -postScript ShowData.java 0xa4314 0xa4318

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.data.DataType;
import ghidra.program.model.listing.Data;
import ghidra.program.model.mem.Memory;

public class ShowData extends GhidraScript {
    @Override
    protected void run() throws Exception {
        Memory memory = currentProgram.getMemory();
        for (String value : getScriptArgs()) {
            Address address = toAddr(Long.decode(value));
            Data data = getDataAt(address);
            long raw = Integer.toUnsignedLong(memory.getInt(address));
            Address target = toAddr(raw);
            Data targetData = getDataAt(target);
            println(address + " raw=0x" + Long.toHexString(raw) +
                " data=" + (data == null ? "<none>" : data.toString()) +
                " target=" + target +
                " targetData=" + (targetData == null ? "<none>" : targetData.toString()));
        }
    }
}
