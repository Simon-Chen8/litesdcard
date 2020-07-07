# This file is Copyright (c) 2017-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2017 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

from functools import reduce
from operator import or_

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer
from migen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.build.io import SDRInput, SDROutput, SDRTristate

from litex.soc.interconnect.csr import *
from litex.soc.interconnect import stream

from litesdcard.common import *

# Pads ---------------------------------------------------------------------------------------------

_sdpads_layout = [
    ("clk", 1),
    ("cmd", [
        ("i",  1),
        ("o",  1),
        ("oe", 1)
    ]),
    ("data", [
        ("i",  4),
        ("o",  4),
        ("oe", 1)
    ]),
]

# SDCard PHY Clocker -------------------------------------------------------------------------------

class SDPHYClocker(Module, AutoCSR):
    def __init__(self, with_reset_synchronizer=True):
        self.enable  = CSRStorage()
        self.divider = CSRStorage(8, reset=128)

        # # #

        self.clock_domains.cd_sd = ClockDomain()
        if with_reset_synchronizer:
            self.specials += AsyncResetSynchronizer(self.cd_sd, ~self.enable.storage)
        else:
            self.comb += self.cd_sd.rst.eq(~self.enable.storage)

        divider = Signal(8)
        self.sync += divider.eq(divider + 1)

        cases = {}
        cases["default"] = self.cd_sd.clk.eq(divider[0])
        for i in range(1, 8):
            cases[2**i] = self.cd_sd.clk.eq(divider[i-1])
        self.comb += Case(self.divider.storage, cases)

# SDCard PHY Read ----------------------------------------------------------------------------------

@ResetInserter()
class SDPHYR(Module):
    def __init__(self, cmd=False, data=False, data_width=1, skip_start_bit=False):
        assert cmd or data
        self.pads_in  = pads_in = stream.Endpoint(_sdpads_layout)
        self.source   = source  = stream.Endpoint([("data", 8)])

        # # #

        pads_in_data = pads_in.cmd.i[:data_width] if cmd else pads_in.data.i[:data_width]

        # Xfer starts when data == 0
        start = Signal()
        run   = Signal()
        self.comb += start.eq(pads_in_data == 0)
        self.sync.sd += run.eq(start | run)

        # Convert data to 8-bit stream
        converter = stream.Converter(data_width, 8, reverse=True)
        converter = ClockDomainsRenamer("sd")(converter)
        buf       = stream.Buffer([("data", 8)])
        buf       = ClockDomainsRenamer("sd")(buf)
        self.submodules += converter, buf
        self.comb += [
            converter.sink.valid.eq(run if skip_start_bit else (start | run)),
            converter.sink.data.eq(pads_in_data),
            converter.source.connect(buf.sink),
            buf.source.connect(source)
        ]

# SDCard PHY Init ----------------------------------------------------------------------------------

class SDPHYInit(Module, AutoCSR):
    def __init__(self):
        self.initialize  = CSR()
        self.pads_in  = pads_in  = stream.Endpoint(_sdpads_layout)
        self.pads_out = pads_out = stream.Endpoint(_sdpads_layout)

        # # #

        ps_initialize = PulseSynchronizer("sys", "sd")
        self.submodules += ps_initialize
        self.comb += ps_initialize.i.eq(self.initialize.re)

        count = Signal(8)
        fsm = FSM(reset_state="IDLE")
        fsm = ClockDomainsRenamer("sd")(fsm)
        self.submodules += fsm
        fsm.act("IDLE",
            NextValue(count, 0),
            If(ps_initialize.o,
                NextState("INITIALIZE")
            )
        )
        fsm.act("INITIALIZE",
            pads_out.clk.eq(1),
            pads_out.cmd.oe.eq(1),
            pads_out.cmd.o.eq(1),
            pads_out.data.oe.eq(1),
            pads_out.data.o.eq(0b1111),
            NextValue(count, count + 1),
            If(count == (80-1),
                 NextState("IDLE")
            )
        )

# SDCard PHY Command Write -------------------------------------------------------------------------

class SDPHYCMDW(Module):
    def __init__(self):
        self.pads_in  = pads_in  = stream.Endpoint(_sdpads_layout)
        self.pads_out = pads_out = stream.Endpoint(_sdpads_layout)
        self.sink     = sink     = stream.Endpoint([("data", 8)])

        self.done = Signal()

        # # #

        self.submodules.sink_cdc = stream.ClockDomainCrossing(sink.description, "sys", "sd")
        self.comb += sink.connect(self.sink_cdc.sink)
        sink = self.sink_cdc.source

        count       = Signal(8)
        fsm = FSM(reset_state="IDLE")
        fsm = ClockDomainsRenamer("sd")(fsm)
        self.submodules += fsm
        fsm.act("IDLE",
            NextValue(count, 0),
            If(sink.valid,
                NextState("WRITE")
            ).Else(
                self.done.eq(1),
            )
        )
        fsm.act("WRITE",
            pads_out.clk.eq(1),
            pads_out.cmd.oe.eq(1),
            Case(count, {i: pads_out.cmd.o.eq(sink.data[8-1-i]) for i in range(8)}),
            NextValue(count, count + 1),
            If(count == (8-1),
                If(sink.last,
                    NextState("CLK8")
                ).Else(
                    sink.ready.eq(1),
                    NextState("IDLE")
                )
            )
        )
        fsm.act("CLK8",
            pads_out.clk.eq(1),
            pads_out.cmd.oe.eq(1),
            pads_out.cmd.o.eq(1),
            NextValue(count, count + 1),
            If(count == (8-1),
                sink.ready.eq(1),
                NextState("IDLE")
            )
        )

# SDCard PHY Command Read --------------------------------------------------------------------------

class SDPHYCMDR(Module):
    def __init__(self, sys_clk_freq, cmd_timeout, cmdw):
        self.pads_in  = pads_in  = stream.Endpoint(_sdpads_layout)
        self.pads_out = pads_out = stream.Endpoint(_sdpads_layout)
        self.sink    = sink   = stream.Endpoint([("length", 8)])
        self.source  = source = stream.Endpoint([("data", 8), ("status", 3)])

        # # #

        self.submodules.sink_cdc = stream.ClockDomainCrossing(sink.description, "sys", "sd")
        self.comb += sink.connect(self.sink_cdc.sink)
        sink = self.sink_cdc.source

        self.submodules.source_cdc = stream.ClockDomainCrossing(source.description, "sd", "sys")
        self.comb += self.source_cdc.source.connect(source)
        source = self.source_cdc.sink

        timeout = Signal(32, reset=int(cmd_timeout*sys_clk_freq))
        count   = Signal(8)

        cmdr = SDPHYR(cmd=True, data_width=1, skip_start_bit=False)
        cmdr = ClockDomainsRenamer("sd")(cmdr)
        self.comb += pads_in.connect(cmdr.pads_in)
        fsm  = FSM(reset_state="IDLE")
        fsm  = ClockDomainsRenamer("sd")(fsm)
        self.submodules += cmdr, fsm
        fsm.act("IDLE",
            NextValue(count,   0),
            NextValue(timeout, timeout.reset),
            If(sink.valid & cmdw.done,
                NextValue(cmdr.reset, 1),
                NextState("WAIT"),
            )
        )
        fsm.act("WAIT",
            pads_out.clk.eq(1),
            NextValue(cmdr.reset, 0),
            If(cmdr.source.valid,
                NextState("CMD")
            ),
            NextValue(timeout, timeout - 1),
            If(timeout == 0,
                sink.ready.eq(1),
                NextState("TIMEOUT")
            )
        )
        fsm.act("CMD",
            pads_out.clk.eq(1),
            source.valid.eq(cmdr.source.valid),
            source.status.eq(SDCARD_STREAM_STATUS_OK),
            source.last.eq(count == (sink.length - 1)),
            source.data.eq(cmdr.source.data),
            If(source.valid & source.ready,
                cmdr.source.ready.eq(1),
                NextValue(count, count + 1),
                If(source.last,
                    sink.ready.eq(1),
                    If(sink.last,
                        NextValue(count, 0),
                        NextState("CLK8")
                    ).Else(
                        NextState("IDLE")
                    )
                )
            ),
            NextValue(timeout, timeout - 1),
            If(timeout == 0,
                sink.ready.eq(1),
                NextState("TIMEOUT")
            ),
        )
        fsm.act("CLK8",
            pads_out.clk.eq(1),
            pads_out.cmd.oe.eq(1),
            pads_out.cmd.o.eq(1),
            NextValue(count, count + 1),
            If(count == (8-1),
                NextState("IDLE")
            )
        )
        fsm.act("TIMEOUT",
            source.valid.eq(1),
            source.status.eq(SDCARD_STREAM_STATUS_TIMEOUT),
            source.last.eq(1),
            If(source.valid & source.ready,
                NextState("IDLE")
            )
        )

# SDCard PHY CRC Response --------------------------------------------------------------------------

class SDPHYCRCR(Module):
    def __init__(self):
        self.pads_in  = pads_in  = stream.Endpoint(_sdpads_layout)
        self.start = Signal()
        self.valid = Signal()
        self.error = Signal()

        # # #

        crcr = SDPHYR(data=True, data_width=1, skip_start_bit=True)
        crcr = ClockDomainsRenamer("sd")(crcr)
        self.comb += pads_in.connect(crcr.pads_in)
        fsm = FSM(reset_state="IDLE")
        fsm = ClockDomainsRenamer("sd")(fsm)
        self.submodules += crcr, fsm
        fsm.act("IDLE",
            If(self.start,
                NextValue(crcr.reset, 1),
                NextState("WAIT-CHECK")
            )
        )
        fsm.act("WAIT-CHECK",
            NextValue(crcr.reset, 0),
            crcr.source.ready.eq(1),
            If(crcr.source.valid,
                self.valid.eq(crcr.source.data != 0b101),
                self.error.eq(crcr.source.data == 0b101),
                NextState("IDLE")
            )
        )

# SDCard PHY Data Write ----------------------------------------------------------------------------

class SDPHYDATAW(Module):
    def __init__(self):
        self.pads_in  = pads_in  = stream.Endpoint(_sdpads_layout)
        self.pads_out = pads_out = stream.Endpoint(_sdpads_layout)
        self.sink = sink = stream.Endpoint([("data", 8)])

        # # #

        self.submodules.sink_cdc = stream.ClockDomainCrossing(sink.description, "sys", "sd")
        self.comb += sink.connect(self.sink_cdc.sink)
        sink = self.sink_cdc.source

        wrstarted = Signal()
        count     = Signal(8)

        crc = SDPHYCRCR() # FIXME: Report valid/errors to software.
        crc = ClockDomainsRenamer("sd")(crc)
        self.comb += pads_in.connect(crc.pads_in)
        fsm = FSM(reset_state="IDLE")
        fsm = ClockDomainsRenamer("sd")(fsm)
        self.submodules += crc, fsm
        fsm.act("IDLE",
            If(sink.valid,
                pads_out.clk.eq(1),
                pads_out.data.oe.eq(1),
                If(wrstarted,
                    pads_out.data.o.eq(sink.data[4:8]),
                    NextState("DATA")
                ).Else(
                    pads_out.data.o.eq(0),
                    NextState("START")
                )
            )
        )
        fsm.act("START",
            pads_out.clk.eq(1),
            pads_out.data.oe.eq(1),
            pads_out.data.o.eq(sink.data[4:8]),
            NextValue(wrstarted, 1),
            NextState("DATA")
        )
        fsm.act("DATA",
            pads_out.clk.eq(1),
            pads_out.data.oe.eq(1),
            pads_out.data.o.eq(sink.data[0:4]),
            If(sink.last,
                NextState("STOP")
            ).Else(
                sink.ready.eq(1),
                NextState("IDLE")
            )
        )
        fsm.act("STOP",
            pads_out.clk.eq(1),
            pads_out.data.oe.eq(1),
            pads_out.data.o.eq(0b1111),
            NextValue(wrstarted, 0),
            crc.start.eq(1),
            NextState("RESPONSE")
        )
        fsm.act("RESPONSE",
            pads_out.clk.eq(1),
            If(count < 16,
                NextValue(count, count + 1),
            ).Else(
                # wait while busy
                If(pads_in.data.i[0],
                    NextValue(count, 0),
                    sink.ready.eq(1),
                    NextState("IDLE")
                )
            )
        )

# SDCard PHY Data Read -----------------------------------------------------------------------------

class SDPHYDATAR(Module):
    def __init__(self, sys_clk_freq, data_timeout):
        self.pads_in  = pads_in  = stream.Endpoint(_sdpads_layout)
        self.pads_out = pads_out = stream.Endpoint(_sdpads_layout)
        self.sink   = sink   = stream.Endpoint([("block_length", 10)])
        self.source = source = stream.Endpoint([("data", 8), ("status", 3)])

        # # #

        self.submodules.sink_cdc = stream.ClockDomainCrossing(sink.description, "sys", "sd")
        self.comb += sink.connect(self.sink_cdc.sink)
        sink = self.sink_cdc.source

        self.submodules.source_cdc = stream.ClockDomainCrossing(source.description, "sd", "sys")
        self.comb += self.source_cdc.source.connect(source)
        source = self.source_cdc.sink

        timeout = Signal(32, reset=int(data_timeout*sys_clk_freq))
        count   = Signal(10)

        datar = SDPHYR(data=True, data_width=4, skip_start_bit=True)
        datar = ClockDomainsRenamer("sd")(datar)
        self.comb += pads_in.connect(datar.pads_in)
        fsm = FSM(reset_state="IDLE")
        fsm = ClockDomainsRenamer("sd")(fsm)
        self.submodules += datar, fsm
        fsm.act("IDLE",
            NextValue(count, 0),
            If(sink.valid,
                pads_out.clk.eq(1),
                NextValue(timeout, timeout.reset),
                NextValue(count, 0),
                NextValue(datar.reset, 1),
                NextState("WAIT")
            )
        )
        fsm.act("WAIT",
            pads_out.clk.eq(1),
            NextValue(datar.reset, 0),
            NextValue(timeout, timeout - 1),
            If(datar.source.valid,
                NextState("DATA")
            ),
            NextValue(timeout, timeout - 1),
            If(timeout == 0,
                sink.ready.eq(1),
                NextState("TIMEOUT")
            )
        )
        fsm.act("DATA",
            pads_out.clk.eq(1),
            source.valid.eq(datar.source.valid),
            source.status.eq(SDCARD_STREAM_STATUS_OK),
            source.last.eq(count == (sink.block_length + 8 - 1)), # 1 block + 64-bit CRC
            source.data.eq(datar.source.data),
            If(source.valid & source.ready,
                datar.source.ready.eq(1),
                NextValue(count, count + 1),
                If(source.last,
                    sink.ready.eq(1),
                    If(sink.last,
                        NextValue(count, 0),
                        NextState("CLK40")
                    ).Else(
                        NextState("IDLE")
                    )
                )
            ),
            NextValue(timeout, timeout - 1),
            If(timeout == 0,
                sink.ready.eq(1),
                NextState("TIMEOUT")
            )
        )
        fsm.act("CLK40",
            pads_out.clk.eq(1),
            NextValue(count, count + 1),
            If(count == (40-1),
                NextState("IDLE")
            )
        )
        fsm.act("TIMEOUT",
            source.valid.eq(1),
            source.status.eq(SDCARD_STREAM_STATUS_TIMEOUT),
            source.last.eq(1),
            If(source.valid & source.ready,
                NextState("IDLE")
            )
        )

# SDCard PHY IO ------------------------------------------------------------------------------------

class SDPHYIOGen(Module):
    def __init__(self, sdpads, pads):
        # Rst
        if hasattr(sdcard_pads, "rst"):
            self.comb += pads.rst.eq(0)

        # Clk
        self.specials += SDROutput(i=(sdpads.clk & ~ClockSignal("sd")), o=pads.clk)

        # Cmd
        self.specials += SDRTristate(
            io  = pads.cmd,
            o   = sdpads.cmd.o,
            oe  = sdpads.cmd.oe,
            i   = sdpads.cmd.i,
            clk = ClockSignal("sd"),
        )

        # Data
        for i in range(4):
            self.specials += SDRTristate(
                io  = pads.data[i],
                o   = sdpads.data.o[i],
                oe  = sdpads.data.oe,
                i   = sdpads.data.i[i],
                clk = ClockSignal("sd"),
            )

# SDCard PHY Emulator ------------------------------------------------------------------------------

class SDPHYIOEmulator(Module):
    def __init__(self, sdpads, pads):
        # Clk
        self.comb += If(sdpads.clk, pads.clk.eq(~ClockSignal("sd")))

        # Cmd
        self.comb += [
            pads.cmd_i.eq(1),
            If(sdpads.cmd.oe, pads.cmd_i.eq(sdpads.cmd.o)),
            sdpads.cmd.i.eq(1),
            If(~pads.cmd_t, sdpads.cmd.i.eq(pads.cmd_o)),
        ]

        # Data
        self.comb += [
            pads.dat_i.eq(0b1111),
            If(sdpads.data.oe, pads.dat_i.eq(sdpads.data.o)),
            sdpads.data.i.eq(0b1111),
        ]
        for i in range(4):
            self.comb += If(~pads.dat_t[i], sdpads.data.i[i].eq(pads.dat_o[i]))

# SDCard PHY ---------------------------------------------------------------------------------------

class SDPHY(Module, AutoCSR):
    def __init__(self, pads, device, sys_clk_freq, cmd_timeout=5e-3, data_timeout=5e-3):
        use_emulator = hasattr(pads, "cmd_t") and hasattr(pads, "dat_t")
        self.card_detect = CSRStatus() # Assume SDCard is present if no cd pin.
        self.comb += self.card_detect.status.eq(getattr(pads, "cd", 0))

        self.submodules.clocker = clocker = SDPHYClocker(with_reset_synchronizer=not use_emulator)
        self.submodules.init    = init    = SDPHYInit()
        self.submodules.cmdw    = cmdw    = SDPHYCMDW()
        self.submodules.cmdr    = cmdr    = SDPHYCMDR(sys_clk_freq, cmd_timeout, cmdw)
        self.submodules.dataw   = dataw   = SDPHYDATAW()
        self.submodules.datar   = datar   = SDPHYDATAR(sys_clk_freq, data_timeout)

        # # #

        self.sdpads = sdpads = Record(_sdpads_layout)

        # IOs
        sdphy_io_cls = SDPHYIOEmulator if use_emulator else SDPHYIOGen
        self.submodules.io = sdphy_io_cls(sdpads, pads)

        # Connect pads_out of submodules to physical pads ----------------------------------------
        self.comb += [
            sdpads.clk.eq(    reduce(or_, [m.pads_out.clk     for m in [init, cmdw, cmdr, dataw, datar]])),
            sdpads.cmd.oe.eq( reduce(or_, [m.pads_out.cmd.oe  for m in [init, cmdw, cmdr, dataw, datar]])),
            sdpads.cmd.o.eq(  reduce(or_, [m.pads_out.cmd.o   for m in [init, cmdw, cmdr, dataw, datar]])),
            sdpads.data.oe.eq(reduce(or_, [m.pads_out.data.oe for m in [init, cmdw, cmdr, dataw, datar]])),
            sdpads.data.o.eq( reduce(or_, [m.pads_out.data.o  for m in [init, cmdw, cmdr, dataw, datar]])),
        ]

        # Connect physical pads to pads_in of submodules -------------------------------------------
        for m in [init, cmdw, cmdr, dataw, datar]:
            self.comb += m.pads_in.cmd.i.eq(sdpads.cmd.i)
            self.comb += m.pads_in.data.i.eq(sdpads.data.i)
