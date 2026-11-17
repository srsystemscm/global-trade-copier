//+------------------------------------------------------------------+
//| FxdMaeEntryTest.mq4                                               |
//|                                                                    |
//| A SCRIPT (not an EA) that places one trade the way the fxDreema    |
//| mt4-1400 EA places its leg, then performs that EA's MAE            |
//| stop-widening modify. Purpose: drive the copier with a master      |
//| signal pair (OPEN then MODIFY) whose shape matches the real EA,    |
//| without waiting for the EA's own entry conditions to fire.         |
//|                                                                    |
//| Mirrors these fxDreema blocks, with that project's defaults:       |
//|                                                                    |
//|   841 ATR SL LONG     ATR_STP_LS = ATR_SL_LONG * iATR(ATR_LONG,   |
//|   (26 for shorts)                  BASE_TF, ACTIVE_BAR_ENTRY_ID)  |
//|   840 ATR TP LONG     ATR_1X     = ATR_TP_LONG * the same iATR    |
//|   3   BUY MKT FIXED A VolumeMode "fixedRisk" on FIXED_RISK_A,     |
//|   (769 SELL ...)      StopLossMode "dynamicDigits" on ATR_STP_LS  |
//|   4315 MAE INCREASE   SL_INCREASE = MAE_DISTANCE                  |
//|   4302 SL MOVE / MAE  NewSLTPmode "percent", RelativeTo open      |
//|   (4319 for shorts)   price: SL = OP - (OP - oldSL) * pct / 100   |
//|                                                                    |
//| Two consequences of those defaults worth knowing before you read   |
//| the numbers it prints:                                             |
//|                                                                    |
//| - TP_1_LONG_ADJUST is 0.0, so the EA opens with NO take-profit.    |
//|   The TP is computed and then multiplied by zero. That is the      |
//|   project's default, not an oversight here.                        |
//| - MAE_DISTANCE is a PERCENTAGE of the original stop distance, and  |
//|   at 200 it does not tighten the stop, it DOUBLES the distance:    |
//|   OP - 2 * (OP - oldSL). That is what the MAE test mode is for --  |
//|   widening the stop to find how far price runs against a trade     |
//|   before it works out.                                             |
//|                                                                    |
//| The ATR shift is 1 (the last CLOSED bar), which is what blocks     |
//| 1431 -> 1418 resolve ACTIVE_BAR_ENTRY_ID_LONG to under EXEC_MODE 1.|
//| It does not read the forming bar, so repeated runs on one bar give |
//| the same size and stop.                                            |
//|                                                                    |
//| ONE DELIBERATE DIVERGENCE FROM THE EA, added in 1.01:              |
//|                                                                    |
//| ACCT_RISK_MODE defaults to 1 -- percent of BALANCE -- at RISK_A    |
//| 2.0, where the EA's own default is mode 0, a flat $2000. This is a |
//| test setting and not the EA's: a fixed cash risk sizes differently |
//| on every account it is dropped on, so the same script on ten       |
//| charts would risk ten different fractions. Set ACCT_RISK_MODE back |
//| to 0 to get the EA's real default.                                 |
//|                                                                    |
//| It is 2% PER CHART. The script knows nothing of the other charts   |
//| it is running on, so N charts started together risk N% between     |
//| them, and the slaves re-size independently of all of it.           |
//|                                                                    |
//| Versions:                                                          |
//|   1.00 2026-11-11 initial build                                    |
//|   1.03 2026-11-16 RISK_A 1.0 -> 2.0, because 2% of balance is what  |
//|                   the strategies actually send: "most EA are going  |
//|                   to send a 2% risk of balance on signal". A test   |
//|                   that stakes half of that measures the copier's    |
//|                   sizing against a signal it will not see.          |
//|   1.02 2026-11-16 hold a run long enough for the order splitter to  |
//|                   place every slice (CloseAfterMs 300000 -> 30000)  |
//|   1.01 2026-11-13 risk 1% of balance by default, and say in the    |
//|                   log what that came to in cash                    |
//+------------------------------------------------------------------+
//|   1.02 2026-11-15 body moved to FxdMaeEntryTest.mqh so that one    |
//|                   script could exist per timeframe. THIS FILE'S    |
//|                   DEFAULTS ARE UNCHANGED -- D1 and magic 1001 --   |
//|                   so anything already compiled still behaves the   |
//|                   same. For testing the magic -> ATR rule use      |
//|                   FxdMaeEntryTest_D / _H4 / _H1, whose magics      |
//|                   claim the timeframe they measure.                |
//|                                                                    |
//| Needs FxdMaeEntryTest.mqh in the SAME folder.                      |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs
#property version "1.02"

#define MAE_SCRIPT_VERSION "1.03"
#define MAE_BASE_TF        PERIOD_D1
#define MAE_MAGIC_START    1001

#include "FxdMaeEntryTest.mqh"
