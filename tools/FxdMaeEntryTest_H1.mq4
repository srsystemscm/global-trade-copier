//+------------------------------------------------------------------+
//| FxdMaeEntryTest_H1.mq4                             |
//|                                                                    |
//| One-hour. The widest gap from daily, so the easiest mismatch to see.|
//|                                                                    |
//| One of four thin wrappers around FxdMaeEntryTest.mqh, which holds  |
//| the whole body. This file sets only what makes it H1:       |
//|                                                                    |
//|   BASE_TF     PERIOD_H1  the ATR this script sizes its stop from  |
//|   MagicStart  321003     leading digit 3 -- the slave reads H1 |
//|                                                                    |
//| WHAT IT TESTS. The master says "my stop sat 1.5 ATRs away"; the    |
//| slave measures that ATR itself, on the timeframe it takes from the |
//| magic's leading digit. Pair a H1 ATR here with a magic that     |
//| claims H1, and a correct slave reproduces this script's stop.   |
//| A slave reading the wrong timeframe produces a visibly different   |
//| one -- and the run prints all four candidates, so you can read off |
//| which it used instead of working it out.                           |
//|                                                                    |
//| The trailing digits are the same across D/H4/H1 (x21003), so the   |
//| three runs are directly comparable on the slave side.              |
//|                                                                    |
//| Put this file and FxdMaeEntryTest.mqh in the SAME folder --        |
//| MQL4/Scripts -- and MT4 resolves the include with no setup.        |
//|                                                                    |
//| Versions:                                                          |
//|   1.02 2026-11-15 split from FxdMaeEntryTest.mq4 1.01; body shared |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs
#property version "1.02"

#define MAE_SCRIPT_VERSION "1.02-H1"
#define MAE_BASE_TF        PERIOD_H1
#define MAE_MAGIC_START    321003

#include "FxdMaeEntryTest.mqh"
