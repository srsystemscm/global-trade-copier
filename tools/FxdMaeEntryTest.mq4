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
//| 1.0, where the EA's own default is mode 0, a flat $2000. This is a |
//| test setting and not the EA's: a fixed cash risk sizes differently |
//| on every account it is dropped on, so the same script on ten       |
//| charts would risk ten different fractions. Set ACCT_RISK_MODE back |
//| to 0 to get the EA's real default.                                 |
//|                                                                    |
//| It is 1% PER CHART. The script knows nothing of the other charts   |
//| it is running on, so N charts started together risk N% between     |
//| them, and the slaves re-size independently of all of it.           |
//|                                                                    |
//| Versions:                                                          |
//|   1.00 2026-11-11 initial build                                    |
//|   1.01 2026-11-13 risk 1% of balance by default, and say in the    |
//|                   log what that came to in cash                    |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs
#property version "1.01"

#define SCRIPT_VERSION "1.01"

//--- which leg of the EA to imitate
input bool   GoLong              = true;    // false = the short leg (blocks 769 / 4319)

//--- fxDreema mt4-1400 defaults, reproduced verbatim
input ENUM_TIMEFRAMES BASE_TF    = PERIOD_D1;
input int    ATR_PERIOD          = 14;      // ATR_LONG / ATR_SHORT
input int    ATR_SHIFT           = 1;       // ACTIVE_BAR_ENTRY_ID_* -> last closed bar
input double ATR_SL_MULT         = 1.5;     // ATR_SL_LONG / ATR_SL_SHORT
input double ATR_TP_MULT         = 1.0;     // ATR_TP_LONG / ATR_TP_SHORT
input double TP_ADJUST           = 0.0;     // TP_1_LONG_ADJUST -- 0.0 means NO take-profit
input int    ACCT_RISK_MODE      = 1;       // 0 fixed cash, 1 % of balance, 2 % of equity
input int    FIXED_RISK_A        = 2000;    // used when ACCT_RISK_MODE = 0
input double RISK_A              = 1.0;     // PERCENT, when ACCT_RISK_MODE = 1 or 2
input double SPREAD_SLIPPAGE     = 5.0;
input int    MagicStart          = 1001;
input string Group               = "";      // magic = MagicStart + (int)Group
input string MyComment           = "";

//--- the MAE test. 200 = widen the stop to twice its original distance.
input int    MAE_DISTANCE        = 200;
input bool   DoMaeModify         = true;

//--- The master bridge EA diffs open orders every PollIntervalMs (500 by
//--- default). Modifying sooner than one poll means the EA never sees the
//--- original stop and publishes a single OPEN already carrying the widened
//--- one -- no MODIFY signal at all, which is the opposite of the point.
input int    MAE_DELAY_MS        = 1500;

//--- Hold the position this long after the MAE modify, then close it, so a
//--- single run produces the whole OPEN -> MODIFY -> CLOSE sequence the
//--- copier has to carry. 0 leaves the trade open.
input int    CloseAfterMs        = 3000;

//--- fxDreema's POINT_FORMAT_RULES, which decide what a "pip" is
input string POINT_FORMAT_RULES  = "0.001=0.01,0.00001=0.0001,0.000001=0.0001";


//+------------------------------------------------------------------+
void OnStart()
  {
   string symbol = Symbol();
   int    digits = (int)MarketInfo(symbol, MODE_DIGITS);

   Print("FxdMaeEntryTest ", SCRIPT_VERSION, " on ", symbol,
         " (", GoLong ? "LONG" : "SHORT", "), account ", AccountNumber());

   if(!IsTradeAllowed())
     {
      Print("ERROR: trading is not allowed. Enable AutoTrading, and tick ",
            "'Allow live trading' in the script's own properties dialog.");
      return;
     }

   //--- blocks 841 / 26: the ATR the whole trade is built from -------------
   double atr = iATR(symbol, BASE_TF, ATR_PERIOD, ATR_SHIFT);
   if(atr <= 0.0)
     {
      Print("ERROR: iATR returned ", atr, " -- not enough ",
            EnumToString(BASE_TF), " history on ", symbol, " to compute it.");
      return;
     }

   double atrStpLs = ATR_SL_MULT * atr;   // v::ATR_STP_LS
   double atr1x    = ATR_TP_MULT * atr;   // v::ATR_1X

   //--- The EA carries the stop as PIPS (StopLossMode "dynamicDigits" runs
   //--- the price distance through toPips), and the sizing then converts it
   //--- back. Doing the same here rather than shortcutting, because the
   //--- round trip through PipValue() is where a 3- or 5-digit symbol would
   //--- change the answer.
   double slPips = ToPips(atrStpLs, symbol);
   double tpPips = ToPips(atr1x, symbol) * TP_ADJUST;   // * 0.0 by default

   //--- block 3 / 769: size the order -------------------------------------
   double lots = DynamicLotsRisk(symbol, slPips);
   if(lots <= 0.0)
     {
      Print("ERROR: computed lot size of ", lots, " -- nothing to send.");
      return;
     }
   lots = AlignLots(symbol, lots);

   //--- market order, stops derived from the fill price --------------------
   int    type  = GoLong ? OP_BUY : OP_SELL;
   double price = GoLong ? MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);
   price = NormalizeDouble(price, digits);

   double sl = AlignStopLoss(symbol, type, price, 0.0, slPips);
   double tp = (tpPips > 0.0) ? AlignTakeProfit(symbol, type, price, tpPips) : 0.0;

   int magic = MagicStart + (int)StringToInteger(Group);

   Print("  ATR(", ATR_PERIOD, ",", EnumToString(BASE_TF), ",shift ", ATR_SHIFT, ") = ",
         DoubleToStr(atr, digits));
   Print("  stop distance = ", DoubleToStr(ATR_SL_MULT, 2), " x ATR = ",
         DoubleToStr(atrStpLs, digits), " (", DoubleToStr(slPips, 1), " pips)");
   //--- Say what the risk came to in money, not just which mode produced
   //--- it. A percentage is the one setting whose consequence you cannot
   //--- read off the input, and the lot size alone will not tell you that
   //--- a stop got widened to the broker minimum underneath it.
   Print("  risk mode ", ACCT_RISK_MODE, " (", RiskDescription(), ") -> ",
         DoubleToStr(lots, 2), " lots");
   if(tp <= 0.0)
      Print("  take-profit: none (TP_ADJUST = ", DoubleToStr(TP_ADJUST, 2), ")");

   int ticket = OrderSend(symbol, type, lots, price,
                          (int)(SPREAD_SLIPPAGE * PipValue(symbol)),
                          sl, tp, MyComment, magic, 0, GoLong ? clrBlue : clrRed);
   if(ticket < 0)
     {
      Print("ERROR: OrderSend failed: ", GetLastError());
      return;
     }

   if(!OrderSelect(ticket, SELECT_BY_TICKET))
     {
      Print("ERROR: opened ticket ", ticket, " but could not select it.");
      return;
     }

   double op    = OrderOpenPrice();
   double oldSl = OrderStopLoss();
   Print("OPEN  ticket=", ticket, " ", DoubleToStr(OrderLots(), 2), " lots @ ",
         DoubleToStr(op, digits), "  sl=", DoubleToStr(oldSl, digits),
         "  tp=", DoubleToStr(OrderTakeProfit(), digits), "  magic=", magic);

   //--- Deliberately no early returns from here on: whatever happens to the
   //--- MAE step, the position still has to be closed, or a skipped modify
   //--- would silently leave a live trade behind.
   if(!DoMaeModify)
      Print("MAE modify skipped (DoMaeModify = false)");
   else if(oldSl <= 0.0)
      Print("MAE modify skipped: the order has no stop to widen.");
   else
     {
      //--- Let the master bridge EA observe the original stop first.
      Sleep(MAE_DELAY_MS);
      DoMaeStep(ticket, op, oldSl, type, symbol, digits);
     }

   if(CloseAfterMs > 0)
     {
      Sleep(CloseAfterMs);
      CloseTicket(ticket, symbol, digits);
     }
   else
      Print("Leaving ticket ", ticket, " open (CloseAfterMs = 0)");
  }

//+------------------------------------------------------------------+
//| blocks 4302 / 4319: NewSLTPmode "percent", RelativeTo "openprice" |
//|   buy : SL = OP - (OP - oldSL) * pct / 100                        |
//|   sell: SL = OP + (oldSL - OP) * pct / 100                        |
//| Both scale the existing stop DISTANCE by pct, which is why 200    |
//| widens rather than tightens.                                      |
//+------------------------------------------------------------------+
void DoMaeStep(int ticket, double op, double oldSl, int type, string symbol, int digits)
  {
   double newSl;
   if(GoLong)
      newSl = op - ((op - oldSl) * MAE_DISTANCE / 100.0);
   else
      newSl = op + ((oldSl - op) * MAE_DISTANCE / 100.0);

   newSl = AlignStopLevel(symbol, type, newSl);

   if(!OrderSelect(ticket, SELECT_BY_TICKET))
     {
      Print("ERROR: could not re-select ticket ", ticket, " for the MAE modify.");
      return;
     }
   if(OrderCloseTime() > 0)
     {
      Print("MAE modify skipped: ticket ", ticket, " already closed (stop hit?).");
      return;
     }

   if(!OrderModify(ticket, OrderOpenPrice(), newSl, OrderTakeProfit(), 0, clrDeepPink))
     {
      Print("ERROR: MAE OrderModify failed: ", GetLastError(),
            " (requested sl=", DoubleToStr(newSl, digits), ")");
      return;
     }

   Print("MAE   ticket=", ticket, " sl ", DoubleToStr(oldSl, digits), " -> ",
         DoubleToStr(newSl, digits),
         "  (distance ", DoubleToStr(MathAbs(op - oldSl), digits), " -> ",
         DoubleToStr(MathAbs(op - newSl), digits),
         ", ", MAE_DISTANCE, "% of the original)");
  }

//+------------------------------------------------------------------+
//| Close at market, so the run ends flat and the copier sees a CLOSE |
//| rather than a position left hanging on both accounts.             |
//+------------------------------------------------------------------+
void CloseTicket(int ticket, string symbol, int digits)
  {
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
     {
      Print("ERROR: could not select ticket ", ticket, " to close it. ",
            "CHECK THE ACCOUNT -- it may still be open.");
      return;
     }
   if(OrderCloseTime() > 0)
     {
      Print("CLOSE skipped: ticket ", ticket, " is already closed (stop hit?).");
      return;
     }

   double price = (OrderType() == OP_BUY) ? MarketInfo(symbol, MODE_BID)
                                          : MarketInfo(symbol, MODE_ASK);

   if(!OrderClose(ticket, OrderLots(), NormalizeDouble(price, digits),
                  (int)(SPREAD_SLIPPAGE * PipValue(symbol)), clrDeepPink))
     {
      Print("ERROR: OrderClose failed: ", GetLastError(),
            " -- ticket ", ticket, " IS STILL OPEN, close it by hand.");
      return;
     }

   if(OrderSelect(ticket, SELECT_BY_TICKET))
      Print("CLOSE ticket=", ticket, " @ ", DoubleToStr(OrderClosePrice(), digits),
            "  profit=", DoubleToStr(OrderProfit(), 2));
   else
      Print("CLOSE ticket=", ticket, " done");
  }


//+------------------------------------------------------------------+
//| What the risk setting actually amounts to on THIS account, in the |
//| deposit currency, so the log answers "1% of what?" by itself.     |
//+------------------------------------------------------------------+
string RiskDescription()
  {
   if(ACCT_RISK_MODE == 1)
      return DoubleToStr(RISK_A, 2) + "% of balance " +
             DoubleToStr(AccountBalance(), 2) + " = " +
             DoubleToStr((RISK_A / 100.0) * AccountBalance(), 2) + " " +
             AccountCurrency();
   if(ACCT_RISK_MODE == 2)
      return DoubleToStr(RISK_A, 2) + "% of equity " +
             DoubleToStr(AccountEquity(), 2) + " = " +
             DoubleToStr((RISK_A / 100.0) * AccountEquity(), 2) + " " +
             AccountCurrency();
   return "fixed " + DoubleToStr((double)FIXED_RISK_A, 2) + " " + AccountCurrency();
  }

//+------------------------------------------------------------------+
//| Sizing -- fxDreema DynamicLots(), the three *Risk branches only.  |
//| All three divide the money at risk by what one lot would lose at  |
//| the stop, so the only difference is where the money comes from.   |
//+------------------------------------------------------------------+
double DynamicLotsRisk(string symbol, double slPips)
  {
   if(slPips <= 0.0)
      return 0.0;

   double tickValue = MarketInfo(symbol, MODE_TICKVALUE);
   double tickSize  = MarketInfo(symbol, MODE_TICKSIZE);
   double point     = MarketInfo(symbol, MODE_POINT);

   if(tickSize <= 0.0)
      return 0.0;

   double lossPerLot = slPips * ((tickValue / tickSize) * point) * PipValue(symbol);
   if(lossPerLot <= 0.0)
      return 0.0;

   if(ACCT_RISK_MODE == 1)
      return ((RISK_A / 100.0) * AccountBalance()) / lossPerLot;
   if(ACCT_RISK_MODE == 2)
      return ((RISK_A / 100.0) * AccountEquity()) / lossPerLot;

   return (double)FIXED_RISK_A / lossPerLot;   // mode 0, "fixedRisk"
  }

//+------------------------------------------------------------------+
double AlignLots(string symbol, double lots)
  {
   double step = MarketInfo(symbol, MODE_LOTSTEP);
   double minL = MarketInfo(symbol, MODE_MINLOT);
   double maxL = MarketInfo(symbol, MODE_MAXLOT);

   if(step > minL)
      minL = step;
   if(step <= 0.0)
      step = 0.01;

   double aligned = MathRound(lots / step) * step;

   // fxDreema clamps rather than refuses. Kept as-is so the size matches
   // what the EA would really have sent, but said out loud, because a clamp
   // up to the broker minimum is a bigger trade than the risk asked for.
   if(aligned < minL)
     {
      Print("  NOTE: ", DoubleToStr(aligned, 2), " lots is below this broker's ",
            "minimum of ", DoubleToStr(minL, 2), " -- raising it, which trades ",
            "MORE risk than ACCT_RISK_MODE asked for.");
      aligned = minL;
     }
   if(aligned > maxL)
     {
      Print("  NOTE: lot size capped at the broker maximum of ", DoubleToStr(maxL, 2));
      aligned = maxL;
     }
   return aligned;
  }

//+------------------------------------------------------------------+
//| fxDreema AlignStopLoss(), reduced to the path this script takes:  |
//| a market order with the stop given as a pip distance.             |
//+------------------------------------------------------------------+
double AlignStopLoss(string symbol, int type, double price, double slLevel, double slPips)
  {
   if(slLevel <= 0.0 && slPips <= 0.0)
      return 0.0;

   int    digits = (int)MarketInfo(symbol, MODE_DIGITS);
   double point  = MarketInfo(symbol, MODE_POINT);
   int    bs     = (type == OP_BUY) ? 1 : -1;

   double level = slLevel;
   if(level <= 0.0)
      level = price - (slPips * PipValue(symbol) * point) * bs;

   return AlignStopLevel(symbol, type, NormalizeDouble(level, digits));
  }

//+------------------------------------------------------------------+
double AlignTakeProfit(string symbol, int type, double price, double tpPips)
  {
   int    digits = (int)MarketInfo(symbol, MODE_DIGITS);
   double point  = MarketInfo(symbol, MODE_POINT);
   int    bs     = (type == OP_BUY) ? 1 : -1;

   return NormalizeDouble(price + (tpPips * PipValue(symbol) * point) * bs, digits);
  }

//+------------------------------------------------------------------+
//| Pull a stop level inside the broker's minimum distance, and snap  |
//| it to the tick. Widening a stop never trips the minimum, so this  |
//| matters for the entry rather than for the MAE modify -- but the   |
//| tick snap matters for both.                                       |
//+------------------------------------------------------------------+
double AlignStopLevel(string symbol, int type, double level)
  {
   if(level <= 0.0)
      return 0.0;

   int    digits   = (int)MarketInfo(symbol, MODE_DIGITS);
   double point    = MarketInfo(symbol, MODE_POINT);
   double ticksize = MarketInfo(symbol, MODE_TICKSIZE);
   double minstops = MarketInfo(symbol, MODE_STOPLEVEL) * point;

   double bidask = (type == OP_BUY) ? MarketInfo(symbol, MODE_BID)
                                    : MarketInfo(symbol, MODE_ASK);
   int bs = (type == OP_BUY) ? 1 : -1;

   double limit = bidask - minstops * bs;   // closest the stop may sit

   if((bs > 0 && level > limit) || (bs < 0 && level < limit))
     {
      Print("  NOTE: stop ", DoubleToStr(level, digits), " is inside this symbol's ",
            "minimum distance of ", DoubleToStr(minstops / point, 0),
            " points; using ", DoubleToStr(limit, digits), " instead.");
      level = limit;
     }

   if(ticksize > 0.0)
      level = MathRound(level / ticksize) * ticksize;

   return NormalizeDouble(level, digits);
  }

//+------------------------------------------------------------------+
//| fxDreema's pip machinery. A "pip" is not the point: on a 5-digit  |
//| symbol POINT_FORMAT_RULES makes it ten points, and the risk sizing|
//| divides by it, so getting this wrong mis-sizes every trade by 10x.|
//+------------------------------------------------------------------+
double CustomPoint(string symbol)
  {
   double point = MarketInfo(symbol, MODE_POINT);

   string rules[];
   StringSplitSimple(POINT_FORMAT_RULES, ',', rules);

   for(int r = 0; r < ArraySize(rules); r++)
     {
      string pair[];
      StringSplitSimple(rules[r], '=', pair);
      if(ArraySize(pair) != 2)
         continue;

      double from = StrToDouble(pair[0]);
      double to   = StrToDouble(pair[1]);
      if(to <= 0.0 || from <= 0.0)
         continue;

      if(MathAbs(point - from) < from / 10.0)
         return to;
     }
   return point;
  }

double PipValue(string symbol)
  {
   double point = MarketInfo(symbol, MODE_POINT);
   if(point <= 0.0)
      return 1.0;
   return CustomPoint(symbol) / point;
  }

double ToPips(double priceDistance, string symbol)
  {
   double point = MarketInfo(symbol, MODE_POINT);
   double denom = PipValue(symbol) * point;
   if(denom <= 0.0)
      return 0.0;
   return priceDistance / denom;
  }

//+------------------------------------------------------------------+
void StringSplitSimple(string text, ushort sep, string &out[])
  {
   ArrayResize(out, 0);
   int start = 0;
   int len   = StringLen(text);

   for(int i = 0; i <= len; i++)
     {
      if(i == len || StringGetChar(text, i) == sep)
        {
         int n = ArraySize(out);
         ArrayResize(out, n + 1);
         out[n] = StringSubstr(text, start, i - start);
         start = i + 1;
        }
     }
  }
//+------------------------------------------------------------------+
