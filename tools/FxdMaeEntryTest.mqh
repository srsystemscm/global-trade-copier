//+------------------------------------------------------------------+
//| FxdMaeEntryTest.mqh                                               |
//|                                                                    |
//| The shared body of the FxdMaeEntryTest scripts. Not compiled on    |
//| its own -- include it from a .mq4 that has already set the three   |
//| defines below.                                                     |
//|                                                                    |
//| Split out of FxdMaeEntryTest.mq4 on 2026-11-15 so that one script  |
//| could exist per timeframe without three copies of 440 lines of     |
//| fxDreema block arithmetic drifting apart. Everything specific to a |
//| timeframe lives in the wrapper; everything about how the EA sizes, |
//| stops and widens lives here.                                       |
//|                                                                    |
//| The wrapper must define, BEFORE including this file:               |
//|                                                                    |
//|   MAE_SCRIPT_VERSION   string, printed on every run                |
//|   MAE_BASE_TF          the ATR timeframe, e.g. PERIOD_H4           |
//|   MAE_MAGIC_START      the magic, whose LEADING DIGIT must name    |
//|                        that same timeframe (1 D, 2 H4, 3 H1, 4 W)  |
//+------------------------------------------------------------------+
#ifndef MAE_SCRIPT_VERSION
#error "include FxdMaeEntryTest.mqh from a wrapper that defines MAE_SCRIPT_VERSION, MAE_BASE_TF and MAE_MAGIC_START"
#endif

#define SCRIPT_VERSION MAE_SCRIPT_VERSION

//--- which leg of the EA to imitate
input bool   GoLong              = true;    // false = the short leg (blocks 769 / 4319)

//--- fxDreema mt4-1400 defaults, reproduced verbatim
input ENUM_TIMEFRAMES BASE_TF    = MAE_BASE_TF;
input int    ATR_PERIOD          = 14;      // ATR_LONG / ATR_SHORT
input int    ATR_SHIFT           = 1;       // ACTIVE_BAR_ENTRY_ID_* -> last closed bar
input double ATR_SL_MULT         = 1.5;     // ATR_SL_LONG / ATR_SL_SHORT
input double ATR_TP_MULT         = 1.0;     // ATR_TP_LONG / ATR_TP_SHORT
input double TP_ADJUST           = 0.0;     // TP_1_LONG_ADJUST -- 0.0 means NO take-profit
input int    ACCT_RISK_MODE      = 1;       // 0 fixed cash, 1 % of balance, 2 % of equity
input int    FIXED_RISK_A        = 2000;    // used when ACCT_RISK_MODE = 0
input double RISK_A              = 2.0;     // PERCENT, when ACCT_RISK_MODE = 1 or 2
input double SPREAD_SLIPPAGE     = 5.0;
input int    MagicStart          = MAE_MAGIC_START;
input string Group               = "";      // magic = MagicStart + (int)Group
input string MyComment           = "";

//--- the MAE test. 200 = widen the stop to twice its original distance.
input int    MAE_DISTANCE        = 200;
input bool   DoMaeModify         = true;

//--- The master bridge EA diffs open orders every PollIntervalMs (500 by
//--- default). Modifying sooner than one poll means the EA never sees the
//--- original stop and publishes a single OPEN already carrying the widened
//--- one -- no MODIFY signal at all, which is the opposite of the point.
//---
//--- NOW 5000, measured. At 1500 it happened for real on 2026-11-17, to
//--- three symbols at once:
//---
//---   USTEC 16:57   OPEN stop 318.1, MODIFY stop 636.1   copied
//---   USTEC 18:16   OPEN stop 670.3, no MODIFY           REFUSED
//---
//--- The copier then sized to a staked risk of 3351 instead of 1908 and
//--- its 2.50% ceiling refused the trade -- correctly, on a signal that
//--- was not true. XTIUSD (0.94 -> 1.80) and XNGUSD (0.06 -> 0.13) went
//--- the same way in the same run.
//---
//--- What changed was this script, not the bridge: sending a size as
//--- SEVERAL orders puts up to five OrderSends in front of this sleep,
//--- and the modify then lands on all of them at once. The margin that
//--- three polls used to give was spent before the sleep began. H1 is
//--- hit first because its stops are tightest, so the widening is the
//--- largest share of the risk.
//---
//--- 5000 is ten polls. The real fix is for the bridge to publish on
//--- first sighting rather than rely on the gap, which is EA work.
input int    MAE_DELAY_MS        = 5000;

//--- Hold the position this long after the MAE modify, then close it, so a
//--- single run produces the whole OPEN -> MODIFY -> CLOSE sequence the
//--- copier has to carry. 0 leaves the trade open.
//---
//--- NOW 300000 -- five minutes -- so a run cleans up after itself while
//--- still giving a resting order time to do something. It was 3000,
//--- which made the whole sequence fit in six seconds and was right while
//--- the thing under test was the SIGNAL path. It is wrong for anything
//--- that has to REST:
//---
//---   an ETF limit outside regular hours cannot fill in six seconds, so
//---   every run ended "cancelled unfilled entry" whatever the copier did
//---
//---   the entry chase re-prices on an interval -- the first step lands
//---   long after a six-second close has already cancelled the order
//---
//---   33 MGC does not fill against a six-second close either, which is
//---   the case the order splitter exists for
//---
//--- NOW 30000 -- thirty seconds -- which is what the ORDER SPLITTER
//--- needs, without tying a terminal up for five minutes to get it.
//---
//--- HOW LONG A RUN HAS TO LAST. A 3x5s split is a ten-second window,
//--- and the hub's own latency is paid before it starts: on 2026-11-16
//--- the XAUUSD signal arrived at 19:19:37 and the gold slice was not
//--- placed until 19:19:41, because ibkr-etf-1 placed its own order
//--- ahead of it. A run closed around ten seconds in got slice 2 and
//--- lost slice 3. Thirty seconds clears the whole schedule with margin.
//---
//--- SHORTENING THE SPLIT INTERVAL DOES NOT HELP. Once the CLOSE is in
//--- the hub's queue, the consume loop reaches it in the same second
//--- whatever the interval is. The trade has to OUTLIVE the latency plus
//--- the window; there is no interval short enough to get round that.
//---
//--- Which also means CLOSING BY HAND cuts the split short. Stopping the
//--- script closes on the way out, so an early stop ends the schedule
//--- too -- the runs that came out at 5 of 15 and 2 of 3 were closed
//--- manually a few seconds in, not held for this long.
//---
//--- Thirty seconds does not cover the entry chase, which is four steps
//--- at sixty: set this to 300000 for that. 3000 tests the signal path
//--- end to end. 0 leaves the position open for good.
//---
//--- The wait is interruptible and closes on the way out, so stopping the
//--- script does not leave a position behind.
input int    CloseAfterMs        = 30000;

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
   int type = GoLong ? OP_BUY : OP_SELL;

   //--- NOT the levels anything is sent with. Each order below prices its
   //--- own stop from its own quote, because each is a separate trade at
   //--- a separate price. This is here only so the line further down can
   //--- say whether there will be a take-profit at all -- a pre-trade
   //--- `sl` and `tp` sitting here would read as the levels in force.
   bool hasTp = (tpPips > 0.0);

   int magic = MagicStart + (int)StringToInteger(Group);

   Print("  ATR(", ATR_PERIOD, ",", EnumToString(BASE_TF), ",shift ", ATR_SHIFT, ") = ",
         DoubleToStr(atr, digits));
   PrintTimeframeProbe(symbol, digits, magic);
   Print("  stop distance = ", DoubleToStr(ATR_SL_MULT, 2), " x ATR = ",
         DoubleToStr(atrStpLs, digits), " (", DoubleToStr(slPips, 1), " pips)");
   //--- Say what the risk came to in money, not just which mode produced
   //--- it. A percentage is the one setting whose consequence you cannot
   //--- read off the input, and the lot size alone will not tell you that
   //--- a stop got widened to the broker minimum underneath it.
   Print("  risk mode ", ACCT_RISK_MODE, " (", RiskDescription(), ") -> ",
         DoubleToStr(lots, 2), " lots");
   if(!hasTp)
      Print("  take-profit: none (TP_ADJUST = ", DoubleToStr(TP_ADJUST, 2), ")");

   //--- ONE ORDER, OR SEVERAL. A size over the broker's per-order
   //--- maximum used to be clamped down to it, which quietly staked less
   //--- than ACCT_RISK_MODE asked for. Sent as separate orders instead,
   //--- which is the default route in the template this imitates.
   double chunks[];
   int pieces = SplitLots(symbol, lots, chunks);
   if(pieces <= 0)
     {
      Print("ERROR: ", DoubleToStr(lots, 2), " lots could not be split into orders.");
      return;
     }
   if(pieces > 1)
      Print("  ", DoubleToStr(lots, 2), " lots is over this broker's per-order maximum of ",
            DoubleToStr(MarketInfo(symbol, MODE_MAXLOT), 2),
            " -- sending it as ", pieces, " orders");

   int    tickets[];
   double ops[];
   double oldSls[];
   ArrayResize(tickets, pieces);
   ArrayResize(ops, pieces);
   ArrayResize(oldSls, pieces);
   int placed = 0;

   for(int pi = 0; pi < pieces; pi++)
     {
      //--- Re-read per order. Each piece is its own trade at its own
      //--- price, and deriving all of them from one pre-trade quote
      //--- would put stops where no fill happened.
      double pxI = GoLong ? MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);
      pxI = NormalizeDouble(pxI, digits);
      double slI = AlignStopLoss(symbol, type, pxI, 0.0, slPips);
      double tpI = (tpPips > 0.0) ? AlignTakeProfit(symbol, type, pxI, tpPips) : 0.0;

      int t = OrderSend(symbol, type, chunks[pi], pxI,
                        (int)(SPREAD_SLIPPAGE * PipValue(symbol)),
                        slI, tpI, MyComment, magic, 0, GoLong ? clrBlue : clrRed);
      if(t < 0)
        {
         //--- Not a return. Whatever opened still has to be modified and
         //--- closed, and bailing out here is how a piece gets left live
         //--- with nothing watching it.
         Print("ERROR: OrderSend ", pi + 1, " of ", pieces, " for ",
               DoubleToStr(chunks[pi], 2), " lots failed: ", GetLastError());
         continue;
        }
      if(!OrderSelect(t, SELECT_BY_TICKET))
        {
         Print("ERROR: opened ticket ", t, " but could not select it.");
         continue;
        }

      tickets[placed] = t;
      ops[placed]     = OrderOpenPrice();
      oldSls[placed]  = OrderStopLoss();
      Print("OPEN  ", (pieces > 1 ? "(" + IntegerToString(placed + 1) + " of " +
            IntegerToString(pieces) + ") " : ""),
            "ticket=", t, " ", DoubleToStr(OrderLots(), 2), " lots @ ",
            DoubleToStr(ops[placed], digits), "  sl=", DoubleToStr(oldSls[placed], digits),
            "  tp=", DoubleToStr(OrderTakeProfit(), digits), "  magic=", magic);
      placed++;
     }

   if(placed <= 0)
     {
      Print("ERROR: nothing opened.");
      return;
     }
   if(placed < pieces)
      Print("  NOTE: only ", placed, " of ", pieces, " orders opened -- the test is ",
            "running on less than the size it sized for.");

   //--- Deliberately no early returns from here on: whatever happens to the
   //--- MAE step, the position still has to be closed, or a skipped modify
   //--- would silently leave a live trade behind.
   if(!DoMaeModify)
      Print("MAE modify skipped (DoMaeModify = false)");
   else
     {
      //--- Let the master bridge EA observe the original stop first. Once,
      //--- not once per order: the delay is for the bridge to see the
      //--- opening stop, and it has seen all of them by now.
      Sleep(MAE_DELAY_MS);
      for(int mi = 0; mi < placed; mi++)
        {
         if(oldSls[mi] <= 0.0)
           {
            Print("MAE modify skipped for ticket ", tickets[mi], ": no stop to widen.");
            continue;
           }
         DoMaeStep(tickets[mi], ops[mi], oldSls[mi], type, symbol, digits);
        }
     }

   if(CloseAfterMs > 0)
     {
      //--- INTERRUPTIBLE, not one long Sleep(). At 3000ms a blind sleep
      //--- was fine; at the minutes these tests now need, a script that
      //--- cannot be stopped is one you have to kill the terminal to get
      //--- out of -- and killing it mid-wait is exactly how a position
      //--- gets left open with nothing watching it.
      //---
      //--- So it waits in one-second steps, and CLOSES ON THE WAY OUT if
      //--- you remove the script. The whole point of a timed close is
      //--- that nothing is left behind; stopping early should not undo
      //--- that.
      int waited = 0;
      bool stopped = false;
      while(waited < CloseAfterMs)
        {
         if(IsStopped())
           {
            stopped = true;
            break;
           }
         int step = (CloseAfterMs - waited > 1000) ? 1000 : (CloseAfterMs - waited);
         Sleep(step);
         waited += step;
        }
      if(stopped)
         Print("Script stopped after ", waited, "ms of ", CloseAfterMs,
               " -- closing ", placed, " ticket(s) anyway rather than leaving them open");
      else
         Print("Held ", placed, " ticket(s) for ", waited, "ms; closing now");
      //--- EVERY piece, and no early exit. One close failing must not
      //--- leave the others open: that is the state this whole branch
      //--- exists to prevent.
      for(int ci = 0; ci < placed; ci++)
         CloseTicket(tickets[ci], symbol, digits);
     }
   else
      Print("Leaving ", placed, " ticket(s) open (CloseAfterMs = 0) -- ",
            "these trades and every slave leg they opened are yours to close");
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
//| deposit currency, so the log answers "2% of what?" by itself.     |
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
   //--- NO LONGER CAPPED HERE. A size over the broker's per-order
   //--- maximum is sent as SEVERAL ORDERS instead -- see SplitLots() --
   //--- because that is what the EA this mirrors does, and a clamp
   //--- silently stakes less than the risk asked for. On ETHUSD the
   //--- maximum is 10.00 lots and 2% of balance wanted more, so every H1
   //--- and H4 run was testing "whatever 10 lots is" rather than 2%.
   return aligned;
  }

//+------------------------------------------------------------------+
//| One size, as the orders it has to be sent in.                     |
//|                                                                   |
//| Returns how many, with each chunk in `chunks`. One entry for any   |
//| size the broker will take in a single order, which is the ordinary |
//| case and the one that must stay unchanged.                         |
//|                                                                    |
//| THE PIECES ARE SEPARATE POSITIONS, each with its own ticket, and   |
//| the bridge publishes one signal per ticket. That is deliberate:    |
//| additional lots via separate orders is the default route in the    |
//| template this imitates. It is NOT the copier's order splitter,     |
//| which is the opposite idea -- N orders collapsing into ONE         |
//| position on the slave.                                             |
//+------------------------------------------------------------------+
int SplitLots(string symbol, double lots, double &chunks[])
  {
   double step = MarketInfo(symbol, MODE_LOTSTEP);
   double minL = MarketInfo(symbol, MODE_MINLOT);
   double maxL = MarketInfo(symbol, MODE_MAXLOT);
   if(step <= 0.0)
      step = 0.01;
   //--- A broker that does not report a maximum is not a broker with no
   //--- maximum, but guessing one would be worse than sending what was
   //--- asked for and letting the server refuse it.
   if(maxL <= 0.0)
      maxL = lots;

   ArrayResize(chunks, 0);
   double left = NormalizeDouble(lots, 2);
   int guard = 0;
   while(left > 0.0000001 && guard < 1000)
     {
      guard++;
      double take = (left > maxL) ? maxL : left;
      take = MathRound(take / step) * step;
      if(take < minL)
         take = minL;
      if(take > left)
         take = left;                      // the last piece
      int n = ArraySize(chunks);
      ArrayResize(chunks, n + 1);
      chunks[n] = NormalizeDouble(take, 2);
      left = NormalizeDouble(left - take, 2);
     }
   return ArraySize(chunks);
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

//+------------------------------------------------------------------+
//| What the slave's stop SHOULD look like, per candidate timeframe.  |
//|                                                                    |
//| The point of the per-timeframe scripts. This one states a stop as  |
//| a multiple of ATR and the slave measures that ATR itself, off the  |
//| timeframe it reads from the magic's leading digit. If it reads the |
//| wrong one, the multiple means something else entirely -- and the   |
//| dollar risk still prints correct, which is what makes it invisible.|
//|                                                                    |
//| So: print the stop distance EVERY candidate timeframe would give.  |
//| Whichever the slave's actual stop matches is the one it used, and  |
//| no arithmetic is needed to tell.                                   |
//+------------------------------------------------------------------+
void PrintTimeframeProbe(string symbol, int digits, int magic)
  {
   ENUM_TIMEFRAMES tfs[4];
   string          names[4];
   int             lead[4];
   tfs[0] = PERIOD_D1; names[0] = "D1";  lead[0] = 1;
   tfs[1] = PERIOD_H4; names[1] = "H4";  lead[1] = 2;
   tfs[2] = PERIOD_H1; names[2] = "H1";  lead[2] = 3;
   tfs[3] = PERIOD_W1; names[3] = "W1";  lead[3] = 4;

   //--- the rule itself: first digit of the magic, however long it is
   string  asText   = IntegerToString(MathAbs(magic));
   int     firstDig = (int)StringToInteger(StringSubstr(asText, 0, 1));
   string  claimed  = "none -- outside the 1-4 scheme, slave default applies";
   for(int k = 0; k < 4; k++)
      if(lead[k] == firstDig)
         claimed = names[k];

   Print("  --- magic -> ATR timeframe ---------------------------------");
   Print("  magic ", magic, " begins with ", firstDig,
         ", so the slave should measure ATR on: ", claimed);

   for(int i = 0; i < 4; i++)
     {
      double a = iATR(symbol, tfs[i], ATR_PERIOD, ATR_SHIFT);
      if(a <= 0.0)
        {
         Print("    ", names[i], ": no ATR -- not enough history on this chart");
         continue;
        }
      string mark = (tfs[i] == BASE_TF) ? "  <-- this script" : "";
      if(names[i] == claimed && tfs[i] != BASE_TF)
         mark = "  <-- the magic claims THIS, and it is not what this script used";
      Print("    ", names[i], ": ATR ", DoubleToStr(a, digits),
            "  ->  stop ", DoubleToStr(ATR_SL_MULT * a, digits),
            " (", DoubleToStr(ToPips(ATR_SL_MULT * a, symbol), 1), " pips)", mark);
     }
   Print("  Compare the slave's stop against those four. The one it ",
         "matches is the timeframe it measured on.");
   Print("  ------------------------------------------------------------");
  }
