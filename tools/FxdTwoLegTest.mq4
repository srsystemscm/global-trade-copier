//+------------------------------------------------------------------+
//| FxdTwoLegTest.mq4                                                 |
//|                                                                    |
//| A SCRIPT (not an EA) that opens TWO legs from one trigger, the way |
//| an fxDreema EA with a target leg and a runner does, then performs  |
//| the MAE stop-widening modify on both and closes both.              |
//|                                                                    |
//| Purpose: drive the copier with a master trigger that produces TWO  |
//| positions rather than one. FxdMaeEntryTest covers the single-leg   |
//| case; this is the one that asks whether each leg is copied,        |
//| modified and closed INDEPENDENTLY on every slave, which is where a |
//| registry keyed on the master ticket gets interesting.              |
//|                                                                    |
//| The two legs differ in exactly one way:                            |
//|                                                                    |
//|   leg 1 ("target")  TP = TP_ATR_MULT x ATR, default 1.0 x ATR      |
//|   leg 2 ("runner")  no TP at all                                   |
//|                                                                    |
//| Same symbol, same direction, same entry, same ATR stop. The runner |
//| reproduces what the real mt4-1400 sends today, since its           |
//| TP_1_LONG_ADJUST is 0.0 and multiplies its computed TP by zero.    |
//| The target leg is what a TP_ADJUST of 1.0 would have produced.     |
//|                                                                    |
//| WHY A TP MATTERS TO THE COPIER, not just to the trade: a leg with  |
//| a take-profit can close itself at the broker, without the master   |
//| EA ever sending a CLOSE. The copier then has to notice a position  |
//| that left by the back door. A leg with no TP can only be closed by |
//| the master. Running both at once puts both paths under one ticket  |
//| pair, which is the point of this script over two runs of the other.|
//|                                                                    |
//| RISK. SplitRiskAcrossLegs (default true) halves the computed size  |
//| so the two legs TOGETHER stake RISK_A, not twice it. That keeps a  |
//| chart's exposure the same as FxdMaeEntryTest's.                    |
//|                                                                    |
//| It does NOT keep the SLAVES' exposure the same. Every slave now    |
//| sizes autonomously from its own balance, per signal -- so two      |
//| signals mean two independently sized positions, each risking the   |
//| slave's own percentage. On the slaves this chart is 2x, whatever   |
//| the master does. That is inherent to testing a two-leg copy and is |
//| said out loud because the master's log will not show it.           |
//|                                                                    |
//| Both legs carry the same magic by default, because one EA emitting |
//| two orders is one strategy. MagicLegOffset gives leg 2 its own, to |
//| test a per-slave magic selection splitting a pair.                 |
//|                                                                    |
//| Versions:                                                          |
//|   1.00 2026-11-13 initial build, from FxdMaeEntryTest 1.01         |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs
#property version "1.00"

#define SCRIPT_VERSION "1.00"

//--- which leg of the EA to imitate
input bool   GoLong              = true;    // false = the short leg

//--- fxDreema mt4-1400 defaults
input ENUM_TIMEFRAMES BASE_TF    = PERIOD_D1;
input int    ATR_PERIOD          = 14;
input int    ATR_SHIFT           = 1;       // last CLOSED bar
input double ATR_SL_MULT         = 1.5;     // stop = 1.5 x ATR
input double TP_ATR_MULT         = 1.0;     // leg 1 take-profit = 1 x ATR
input int    ACCT_RISK_MODE      = 1;       // 0 fixed cash, 1 % balance, 2 % equity
input int    FIXED_RISK_A        = 2000;
input double RISK_A              = 1.0;     // PERCENT, modes 1 and 2
input double SPREAD_SLIPPAGE     = 5.0;
input int    MagicStart          = 1001;
input string Group               = "";
input string MyComment           = "";

//--- 0 = both legs share a magic (one EA, one strategy, two orders).
//--- Non-zero gives leg 2 MagicStart+offset, so a per-slave magic
//--- selection can be tested on a pair that arrives together.
input int    MagicLegOffset      = 0;

//--- true: the two legs SPLIT RISK_A between them. false: each leg
//--- stakes RISK_A in full, so the chart risks twice RISK_A.
input bool   SplitRiskAcrossLegs = true;

//--- the MAE test. 200 = widen the stop to twice its original distance.
input int    MAE_DISTANCE        = 200;
input bool   DoMaeModify         = true;

//--- The master bridge EA diffs open orders every PollIntervalMs (500 by
//--- default). Modifying sooner than one poll means the EA never sees the
//--- original stop and publishes a single OPEN already carrying the
//--- widened one -- no MODIFY signal at all, which is the opposite of the
//--- point. Both legs are opened BEFORE this wait, so one poll covers the
//--- pair and the copier sees them arrive together, as an EA's two orders
//--- really would.
input int    MAE_DELAY_MS        = 1500;

//--- Hold, then close whatever is still open. A leg that hit its TP in
//--- the meantime is already gone, and that is a result, not a failure.
input int    CloseAfterMs        = 3000;

input string POINT_FORMAT_RULES  = "0.001=0.01,0.00001=0.0001,0.000001=0.0001";


//--- leg bookkeeping -------------------------------------------------
int    g_ticket[2];
double g_openPrice[2];
double g_openSl[2];
string g_legName[2] = {"target(TP)", "runner(noTP)"};


//+------------------------------------------------------------------+
void OnStart()
  {
   string symbol = Symbol();
   int    digits = (int)MarketInfo(symbol, MODE_DIGITS);

   for(int i = 0; i < 2; i++)
     {
      g_ticket[i]    = -1;
      g_openPrice[i] = 0.0;
      g_openSl[i]    = 0.0;
     }

   Print("FxdTwoLegTest ", SCRIPT_VERSION, " on ", symbol,
         " (", GoLong ? "LONG" : "SHORT", "), account ", AccountNumber());

   if(!IsTradeAllowed())
     {
      Print("ERROR: trading is not allowed. Enable AutoTrading, and tick ",
            "'Allow live trading' in the script's own properties dialog.");
      return;
     }

   double atr = iATR(symbol, BASE_TF, ATR_PERIOD, ATR_SHIFT);
   if(atr <= 0.0)
     {
      Print("ERROR: iATR returned ", atr, " -- not enough ",
            EnumToString(BASE_TF), " history on ", symbol, " to compute it.");
      return;
     }

   double slPips = ToPips(ATR_SL_MULT * atr, symbol);
   double tpPips = ToPips(TP_ATR_MULT * atr, symbol);

   double lots = DynamicLotsRisk(symbol, slPips);
   if(lots <= 0.0)
     {
      Print("ERROR: computed lot size of ", lots, " -- nothing to send.");
      return;
     }
   if(SplitRiskAcrossLegs)
      lots = lots / 2.0;
   lots = AlignLots(symbol, lots);

   int    type  = GoLong ? OP_BUY : OP_SELL;
   int    magic = MagicStart + (int)StringToInteger(Group);

   Print("  ATR(", ATR_PERIOD, ",", EnumToString(BASE_TF), ",shift ", ATR_SHIFT,
         ") = ", DoubleToStr(atr, digits));
   Print("  stop = ", DoubleToStr(ATR_SL_MULT, 2), " x ATR (",
         DoubleToStr(slPips, 1), " pips);  leg 1 TP = ",
         DoubleToStr(TP_ATR_MULT, 2), " x ATR (", DoubleToStr(tpPips, 1), " pips)");
   Print("  risk mode ", ACCT_RISK_MODE, " (", RiskDescription(), "), ",
         SplitRiskAcrossLegs ? "SPLIT across 2 legs" : "FULL on EACH leg",
         " -> ", DoubleToStr(lots, 2), " lots per leg");

   //--- Both legs go out before the MAE wait, so the bridge's next poll
   //--- sees the pair together rather than as two unrelated entries.
   for(int leg = 0; leg < 2; leg++)
     {
      double price = GoLong ? MarketInfo(symbol, MODE_ASK)
                            : MarketInfo(symbol, MODE_BID);
      price = NormalizeDouble(price, digits);

      double sl = AlignStopLoss(symbol, type, price, 0.0, slPips);
      double tp = (leg == 0 && tpPips > 0.0)
                  ? AlignTakeProfit(symbol, type, price, tpPips) : 0.0;

      int legMagic = magic + ((leg == 1) ? MagicLegOffset : 0);

      int ticket = OrderSend(symbol, type, lots, price,
                             (int)(SPREAD_SLIPPAGE * PipValue(symbol)),
                             sl, tp, MyComment, legMagic, 0,
                             GoLong ? clrBlue : clrRed);
      if(ticket < 0)
        {
         // Not fatal: the other leg may already be live and MUST still be
         // closed. A half-open pair is also a legitimate thing to watch
         // the copier handle.
         Print("ERROR: OrderSend failed for leg ", leg + 1, " (", g_legName[leg],
               "): ", GetLastError());
         continue;
        }

      g_ticket[leg] = ticket;
      if(OrderSelect(ticket, SELECT_BY_TICKET))
        {
         g_openPrice[leg] = OrderOpenPrice();
         g_openSl[leg]    = OrderStopLoss();
         Print("OPEN  leg ", leg + 1, " ", g_legName[leg], " ticket=", ticket,
               " ", DoubleToStr(OrderLots(), 2), " lots @ ",
               DoubleToStr(OrderOpenPrice(), digits),
               "  sl=", DoubleToStr(OrderStopLoss(), digits),
               "  tp=", DoubleToStr(OrderTakeProfit(), digits),
               "  magic=", legMagic);
        }
      else
         Print("OPEN  leg ", leg + 1, " ticket=", ticket,
               " (could not select it to report the fill)");
     }

   if(g_ticket[0] < 0 && g_ticket[1] < 0)
     {
      Print("ERROR: neither leg opened -- nothing to modify or close.");
      return;
     }

   //--- No early returns past here: whatever happens to the MAE step,
   //--- both legs still have to be closed.
   if(!DoMaeModify)
      Print("MAE modify skipped (DoMaeModify = false)");
   else
     {
      Sleep(MAE_DELAY_MS);
      for(int m = 0; m < 2; m++)
         if(g_ticket[m] >= 0 && g_openSl[m] > 0.0)
            DoMaeStep(m, type, symbol, digits);
     }

   if(CloseAfterMs > 0)
     {
      Sleep(CloseAfterMs);
      for(int c = 0; c < 2; c++)
         if(g_ticket[c] >= 0)
            CloseTicket(c, symbol, digits);
     }
   else
      Print("Leaving both legs open (CloseAfterMs = 0)");
  }

//+------------------------------------------------------------------+
//| blocks 4302 / 4319: NewSLTPmode "percent", RelativeTo "openprice" |
//| Both scale the existing stop DISTANCE by pct, which is why 200    |
//| widens rather than tightens. The take-profit is left alone -- the |
//| MAE block moves stops only, and changing the TP here would hide   |
//| whether the copier carried the original one.                      |
//+------------------------------------------------------------------+
void DoMaeStep(int leg, int type, string symbol, int digits)
  {
   int    ticket = g_ticket[leg];
   double op     = g_openPrice[leg];
   double oldSl  = g_openSl[leg];

   double newSl;
   if(GoLong)
      newSl = op - ((op - oldSl) * MAE_DISTANCE / 100.0);
   else
      newSl = op + ((oldSl - op) * MAE_DISTANCE / 100.0);

   newSl = AlignStopLevel(symbol, type, newSl);

   if(!OrderSelect(ticket, SELECT_BY_TICKET))
     {
      Print("ERROR: could not re-select leg ", leg + 1, " ticket ", ticket,
            " for the MAE modify.");
      return;
     }
   if(OrderCloseTime() > 0)
     {
      Print("MAE   leg ", leg + 1, " skipped: ticket ", ticket,
            " already closed (TP or stop hit).");
      return;
     }

   if(!OrderModify(ticket, OrderOpenPrice(), newSl, OrderTakeProfit(), 0, clrDeepPink))
     {
      Print("ERROR: MAE OrderModify failed on leg ", leg + 1, ": ", GetLastError(),
            " (requested sl=", DoubleToStr(newSl, digits), ")");
      return;
     }

   Print("MAE   leg ", leg + 1, " ", g_legName[leg], " ticket=", ticket,
         " sl ", DoubleToStr(oldSl, digits), " -> ", DoubleToStr(newSl, digits),
         "  (distance ", DoubleToStr(MathAbs(op - oldSl), digits), " -> ",
         DoubleToStr(MathAbs(op - newSl), digits), ")");
  }

//+------------------------------------------------------------------+
//| Close at market so the run ends flat. A leg whose take-profit has |
//| already fired is reported as such rather than treated as an error:|
//| that is the back-door exit this script exists to produce, and the |
//| copier's handling of it is the result worth reading.              |
//+------------------------------------------------------------------+
void CloseTicket(int leg, string symbol, int digits)
  {
   int ticket = g_ticket[leg];

   if(!OrderSelect(ticket, SELECT_BY_TICKET))
     {
      Print("ERROR: could not select leg ", leg + 1, " ticket ", ticket,
            " to close it. CHECK THE ACCOUNT -- it may still be open.");
      return;
     }
   if(OrderCloseTime() > 0)
     {
      Print("CLOSE leg ", leg + 1, " ", g_legName[leg], " ticket=", ticket,
            " was ALREADY closed at ", DoubleToStr(OrderClosePrice(), digits),
            " -- it left without a master CLOSE. Check the slaves followed it.");
      return;
     }

   double price = (OrderType() == OP_BUY) ? MarketInfo(symbol, MODE_BID)
                                          : MarketInfo(symbol, MODE_ASK);

   if(!OrderClose(ticket, OrderLots(), NormalizeDouble(price, digits),
                  (int)(SPREAD_SLIPPAGE * PipValue(symbol)), clrDeepPink))
     {
      Print("ERROR: OrderClose failed on leg ", leg + 1, ": ", GetLastError(),
            " -- ticket ", ticket, " IS STILL OPEN, close it by hand.");
      return;
     }

   if(OrderSelect(ticket, SELECT_BY_TICKET))
      Print("CLOSE leg ", leg + 1, " ", g_legName[leg], " ticket=", ticket,
            " @ ", DoubleToStr(OrderClosePrice(), digits),
            "  profit=", DoubleToStr(OrderProfit(), 2));
   else
      Print("CLOSE leg ", leg + 1, " ticket=", ticket, " done");
  }


//+------------------------------------------------------------------+
//| What the risk setting amounts to on THIS account, in the deposit  |
//| currency, so the log answers "1% of what?" by itself.             |
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

   return (double)FIXED_RISK_A / lossPerLot;
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

   if(aligned < minL)
     {
      // Worth saying twice as loudly here as in the single-leg script:
      // SplitRiskAcrossLegs halves the size BEFORE this clamp, so a
      // symbol near the broker minimum ends up with two minimum legs and
      // the pair stakes more than RISK_A, not less.
      Print("  NOTE: ", DoubleToStr(aligned, 2), " lots per leg is below this ",
            "broker's minimum of ", DoubleToStr(minL, 2), " -- raising it. ",
            "With two legs the pair now risks MORE than ACCT_RISK_MODE asked for.");
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

   double limit = bidask - minstops * bs;

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
