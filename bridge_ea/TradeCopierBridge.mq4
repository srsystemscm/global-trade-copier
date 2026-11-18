//+------------------------------------------------------------------+
//| TradeCopierBridge.mq4                                             |
//| Phase 2: real order-state diffing on the master account. Snapshots|
//| every open market order each timer tick, diffs against the last   |
//| snapshot, and publishes OPEN / MODIFY (SL or TP changed) / CLOSE   |
//| signals over ZMQ PUB. Also PUSHes a heartbeat on a fixed interval. |
//|                                                                    |
//| Phase 3: also includes this instrument's own iATR() value in each  |
//| signal. Autonomous slaves (e.g. Schwab) back-calculate what        |
//| multiple of this ATR the SL/TP represent and reapply that multiple |
//| to their own instrument's ATR -- "Option 1" back-calculation, used |
//| until the fxDreema EAs that actually set SL/TP are updated to      |
//| publish their own ATR multiple via GlobalVariableSet().            |
//|                                                                    |
//| Requires the mql-zmq library (https://github.com/dingmaotu/mql-zmq)|
//| - Copy Zmq.mqh (and its dependencies) into MQL4/Include/Zmq/       |
//| - Copy libzmq.dll + libsodium.dll into MQL4/Libraries/              |
//| - Enable "Allow DLL imports" for this EA                           |
//|                                                                    |
//| Versions (bump both lines below with every change):                |
//|   1.00 2026-08-26 initial build                                    |
//|   1.01 2026-09-10 fixes from the first real terminal shakeout      |
//|   1.02 2026-10-29 magic number carried through the signal          |
//|   1.03 2026-11-11 CLOSE publishes the exit price, not the entry    |
//|   1.04 2026-11-14 publish what the order actually staked, and     |
//|                   report this build on the heartbeat               |
//|   1.06 2026-11-16 the ATR is measured on the last CLOSED bar,     |
//|                   which is the bar the EA sized from. Shift 0 was  |
//|                   the forming one, stretching every slave stop --  |
//|                   7% on H1 -- and making a run unrepeatable.       |
//|   1.05 2026-11-15 the ATR follows the MAGIC, not this EA's chart.  |
//|                   Until now every signal carried iATR() on         |
//|                   PERIOD_CURRENT -- one figure for every strategy  |
//|                   -- so an H4 stop arrived at the slave measured   |
//|                   in daily ATRs. See AtrTimeframeForMagic().       |
//+------------------------------------------------------------------+
#property strict
#property version   "1.06"
// MUST match #property version above. MQL has no way to read that from
// code, so the two are kept in step by hand -- and they had already
// drifted once: the property said 1.04 while this said 1.03, which would
// have had every master reporting the wrong build through the very
// handshake added to stop that happening.
#define   BRIDGE_VERSION "1.07"

#include <Zmq/Zmq.mqh>

input string HubHost              = "127.0.0.1";
input int    HubPubPort           = 5555;   // hub SUB socket - we PUB/connect here
input int    HubPushPort          = 5557;   // hub PULL socket - we PUSH/connect here
//--- HOW WIDE THE RACE IS. This EA has no trade event to hook: MT4 has
//--- no OnTradeTransaction, so an order is only ever noticed by polling.
//--- If an order is opened AND modified between two polls, the first
//--- sighting already carries the modified stop and is published as the
//--- OPEN -- with no MODIFY to follow, because by then nothing has
//--- changed.
//---
//--- That is not cosmetic. The hub sizes a copy from the staked risk the
//--- OPEN reports, so a widened stop arriving as the opening one asks
//--- every slave to stake roughly twice what the master did. On
//--- 2026-11-17 it put USTEC, XTIUSD and XNGUSD over mt5-icm-1's 2.50%
//--- ceiling and all three were refused -- correctly, on a signal that
//--- was not true.
//---
//--- 100ms rather than 500 narrows the window five-fold. It cannot close
//--- it: nothing on MT4 can, short of a trade event that does not exist.
//--- What is left is reported instead -- see the late-sighting warning
//--- in DiffAndPublish.
input int    PollIntervalMs       = 100;    // how often to diff open orders
input int    HeartbeatIntervalMs  = 5000;   // how often to send a heartbeat
input double SlTpEpsilon          = 0.00001; // ignore SL/TP deltas smaller than this (float noise)
input int    AtrPeriod            = 14;      // iATR() period used for the "atr" field
input int    AtrTimeframe         = PERIOD_CURRENT; // fallback when the magic names no timeframe
//--- A magic's LEADING DIGIT names the timeframe its strategy runs on:
//--- 1 daily, 2 H4, 3 H1, 4 weekly. The hub applies this rule to the
//--- SLAVE's ATR already; without it here, only half the translation
//--- happens. See the comment above PublishSignal's atr line.
input bool   AtrTimeframeFromMagic = true;   // leading digit of the magic picks the ATR timeframe
//--- Which bar the ATR is measured on. 1 is the last CLOSED bar, which is
//--- what the fxDreema EAs size from (ACTIVE_BAR_ENTRY_ID resolves to 1
//--- under EXEC_MODE 1) and what the FxdMaeEntryTest scripts use.
//---
//--- IT WAS 0 -- the FORMING bar -- so the ATR published alongside a
//--- signal was not the ATR the EA had sized that signal with. The hub
//--- divides the stop distance by this figure to get the multiple it
//--- sends on, so the two disagreeing stretches every slave's stop by
//--- whatever the forming bar happens to differ by. Measured on
//--- 2026-11-16, the MAE step should produce exactly 3.0 ATR on every
//--- timeframe and produced 3.0172 / 3.1145 / 3.2200 on D1 / H4 / H1:
//--- the shorter the bar, the larger the part of it still forming, and
//--- H1 came out 7% wide.
//---
//--- Shift 1 also makes a run REPEATABLE. The forming bar changes tick
//--- by tick, so two identical signals a minute apart published two
//--- different ATRs and sized differently for no reason a reader could
//--- see.
input int    AtrShift             = 1;       // 1 = last closed bar, 0 = the forming one

Context zmqContext("bridge_ea_master");
Socket  pubSocket(zmqContext, ZMQ_PUB);
Socket  pushSocket(zmqContext, ZMQ_PUSH);

struct OrderSnapshot
  {
   int    ticket;
   string symbol;
   int    type;
   double lots;
   double price;
   double sl;
   double tp;
   int    magic;    // OrderMagicNumber() -- identifies which strategy opened it
   string comment;  // OrderComment() -- secondary identifier, free-form
   //--- Server time the order was filled. Only used to notice that this
   //--- EA is seeing it LATE, which means the stop it is carrying may
   //--- already have been modified -- see DiffAndPublish.
   datetime opened;
  };

OrderSnapshot knownOrders[];
// GetTickCount() ms, not a datetime: TimeCurrent() is the last *quote*
// time for the chart symbol and stops advancing when no ticks arrive
// (weekend, closed market, illiquid symbol), which would stall the
// heartbeat and make MasterWatchdog report a healthy master as down.
// uint arithmetic wraps correctly across GetTickCount()'s ~49.7d rollover.
uint lastHeartbeatSent = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   string pubAddr  = StringFormat("tcp://%s:%d", HubHost, HubPubPort);
   string pushAddr = StringFormat("tcp://%s:%d", HubHost, HubPushPort);

   // LINGER=0 so OnDeinit can't block MT4's shutdown trying to flush queued
   // frames to a hub that isn't there. Observed as "shutdown by timeout" in
   // the Experts log (uninit reason 9) when closing the terminal.
   pubSocket.setLinger(0);
   pushSocket.setLinger(0);

   pubSocket.connect(pubAddr);
   pushSocket.connect(pushAddr);

   Print("TradeCopierBridge ", BRIDGE_VERSION, " connected: PUB->", pubAddr, "  PUSH->", pushAddr);

   TakeSnapshot(knownOrders);
   EventSetMillisecondTimer(PollIntervalMs);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   pubSocket.disconnect(StringFormat("tcp://%s:%d", HubHost, HubPubPort));
   pushSocket.disconnect(StringFormat("tcp://%s:%d", HubHost, HubPushPort));
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   OrderSnapshot current[];
   TakeSnapshot(current);
   DiffAndPublish(knownOrders, current);
   CopySnapshots(knownOrders, current);

   if(GetTickCount() - lastHeartbeatSent >= (uint)HeartbeatIntervalMs)
     {
      SendHeartbeat();
      lastHeartbeatSent = GetTickCount();
     }
  }

//+------------------------------------------------------------------+
//| Snapshot every open market order (BUY/SELL only -- pending orders |
//| are out of scope for the copier).                                  |
//+------------------------------------------------------------------+
void TakeSnapshot(OrderSnapshot &out[])
  {
   ArrayResize(out, 0);
   for(int i = 0; i < OrdersTotal(); i++)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(OrderType() != OP_BUY && OrderType() != OP_SELL)
         continue;

      int n = ArraySize(out);
      ArrayResize(out, n + 1);
      out[n].ticket = OrderTicket();
      out[n].symbol = OrderSymbol();
      out[n].type   = OrderType();
      out[n].lots   = OrderLots();
      out[n].price  = OrderOpenPrice();
      out[n].sl     = OrderStopLoss();
      out[n].tp     = OrderTakeProfit();
      // Several strategies can run on one master account and trade the same
      // symbol. The magic number is what tells them apart, and the hub routes
      // on it -- without this every strategy's signals are indistinguishable.
      out[n].magic   = OrderMagicNumber();
      out[n].comment = OrderComment();
      out[n].opened  = OrderOpenTime();
     }
  }

//+------------------------------------------------------------------+
//| MQL4 has no whole-array assignment, and ArrayCopy() refuses any   |
//| struct containing a string (OrderSnapshot.symbol), so the previous |
//| snapshot has to be rebuilt element by element.                     |
//+------------------------------------------------------------------+
void CopySnapshots(OrderSnapshot &dst[], OrderSnapshot &src[])
  {
   int n = ArraySize(src);
   ArrayResize(dst, n);
   for(int i = 0; i < n; i++)
      dst[i] = src[i];
  }

//+------------------------------------------------------------------+
int FindByTicket(OrderSnapshot &arr[], int ticket)
  {
   for(int i = 0; i < ArraySize(arr); i++)
      if(arr[i].ticket == ticket)
         return i;
   return -1;
  }

//+------------------------------------------------------------------+
void DiffAndPublish(OrderSnapshot &prev[], OrderSnapshot &curr[])
  {
   // new or modified
   for(int i = 0; i < ArraySize(curr); i++)
     {
      int prevIdx = FindByTicket(prev, curr[i].ticket);
      if(prevIdx < 0)
        {
         //--- SEEN LATE? Then the stop on it may not be the one it opened
         //--- with, and this OPEN is about to tell the hub a staked risk
         //--- the master never staked.
         //---
         //--- Said here, at the source, because the alternative is what
         //--- happened on 2026-11-17: three symbols refused by a slave's
         //--- risk ceiling, and an hour of comparing OPEN and MODIFY
         //--- stops across two runs to work out why.
         //---
         //--- One second is the resolution of these clocks, so this
         //--- catches the terminal being wedged rather than a tight
         //--- race. That is the case worth catching: at 100ms polls a
         //--- genuine race needs the terminal to be in trouble anyway.
         int lateBy = (int)(TimeCurrent() - curr[i].opened);
         if(curr[i].opened > 0 && lateBy >= 2)
            Print("TradeCopierBridge: WARNING ticket ", curr[i].ticket,
                  " (", curr[i].symbol, ") was first seen ", lateBy,
                  "s after it opened -- if it was modified in that time, the",
                  " stop below is NOT the one it opened with and every copy",
                  " will be sized from it");
         PublishSignal("OPEN", curr[i]);
        }
      else if(MathAbs(curr[i].sl - prev[prevIdx].sl) > SlTpEpsilon ||
              MathAbs(curr[i].tp - prev[prevIdx].tp) > SlTpEpsilon)
        {
         PublishSignal("MODIFY", curr[i]);
        }
     }

   // closed
   for(int i = 0; i < ArraySize(prev); i++)
     {
      if(FindByTicket(curr, prev[i].ticket) >= 0)
         continue;

      // prev[i].price is OrderOpenPrice() -- the ENTRY. Publishing that on a
      // CLOSE reported every exit as its own entry, which is worse than
      // useless because it looks plausible: every round trip appears to have
      // made exactly nothing, and the copy's round-trip variance cannot be
      // measured at all. Found on the first live MT4 -> MT5 run.
      //
      // OrderSelect(SELECT_BY_TICKET) searches the history pool as well as
      // open trades, so once the order is gone from the open list its real
      // exit is there to be read.
      OrderSnapshot closed;
      closed = prev[i];
      closed.price = 0.0;   // 0 = "unknown", which the hub stores as NULL
      if(OrderSelect(prev[i].ticket, SELECT_BY_TICKET) && OrderCloseTime() > 0)
         closed.price = OrderClosePrice();
      else
         Print("TradeCopierBridge: no close price for ticket ", prev[i].ticket,
               " -- reporting it as unknown rather than guessing");

      PublishSignal("CLOSE", closed);
     }
  }

//+------------------------------------------------------------------+
//| The ATR timeframe a magic names, by its LEADING DIGIT.             |
//|                                                                    |
//| 1 daily, 2 H4, 3 H1, 4 weekly -- Chris's structural magic scheme.  |
//| 121003 is a daily system; 221003 would be H4.                      |
//|                                                                    |
//| Kept deliberately identical to the hub's own                       |
//| bar_size_for_magic_prefix() in app/sizing.py. The two run on       |
//| opposite sides of the wire and must agree: the master divides the  |
//| stop by this ATR, the slave multiplies by its own. Disagree, and   |
//| the error is silent -- sizing still reports the intended dollar    |
//| risk while the position is a multiple of the intended size.        |
//|                                                                    |
//| A magic outside 1-4 falls back to AtrTimeframe, which is what      |
//| every signal used before this existed.                             |
//+------------------------------------------------------------------+
int AtrTimeframeForMagic(int magic)
  {
   if(!AtrTimeframeFromMagic)
      return(AtrTimeframe);

   //--- The leading digit, however many digits the magic has. MathAbs
   //--- first: a negative magic is not expected, but reading '-' as the
   //--- leading character would silently fall back for the whole strategy.
   string asText = IntegerToString(MathAbs(magic));
   if(StringLen(asText) == 0)
      return(AtrTimeframe);

   string firstChar = StringSubstr(asText, 0, 1);
   if(firstChar == "1") return(PERIOD_D1);
   if(firstChar == "2") return(PERIOD_H4);
   if(firstChar == "3") return(PERIOD_H1);
   if(firstChar == "4") return(PERIOD_W1);

   return(AtrTimeframe);
  }

//+------------------------------------------------------------------+
void PublishSignal(string action, OrderSnapshot &o)
  {
   string direction = (o.type == OP_BUY) ? "BUY" : "SELL";

   //--- THE OTHER HALF OF THE MAGIC -> ATR RULE.
   //---
   //--- This was iATR(o.symbol, AtrTimeframe, ...) with AtrTimeframe at
   //--- PERIOD_CURRENT -- the chart THIS EA happens to sit on. Every
   //--- signal from every strategy therefore carried the same ATR, and
   //--- the hub divides the stop distance by it to get the multiple it
   //--- sends on. So an H4 strategy's stop, 1.5 H4-ATRs wide, arrived at
   //--- the slave as 0.5 DAILY-ATRs, and the slave -- which does read the
   //--- magic and measures its own ATR on H4 -- multiplied that 0.5 by
   //--- its H4 figure. Stop three times too tight, position three times
   //--- too large, dollar risk still reading correct.
   //---
   //--- Measured on 2026-11-15 with FxdMaeEntryTest _D and _H4 on XAUUSD:
   //---   hub ATR     94.42 (D) vs 94.41 (H4)  -- identical, the bug
   //---   slave ATR   81.60 (D) vs 42.34 (H4)  -- correctly different
   //--- H4 opened 46 MGC where it should have opened about 15, and the
   //--- H1 run was refused outright on margin at 223 contracts.
   //---
   //--- The rule is the hub's, matched exactly so the two halves cannot
   //--- drift: leading digit 1 daily, 2 H4, 3 H1, 4 weekly, anything else
   //--- falls back to AtrTimeframe as before.
   int    atrTf = AtrTimeframeForMagic(o.magic);
   double atr   = iATR(o.symbol, atrTf, AtrPeriod, AtrShift);

   // What this order actually put at risk, and what it was risked against.
   //
   // MEASURED, not declared. The EA's own setting says it intends 2% of
   // the account; this says what the order it sent really stakes if the
   // stop is hit. The two differ whenever a lot size was rounded, a stop
   // was pulled to the broker's minimum distance, or somebody changed the
   // EA and forgot. A declared figure cannot notice any of that, and a
   // slave sizing from one would be trusting a number nothing checks --
   // the shape of mistake that made the Flex join match 0 of 109 rows.
   //
   // Only this EA can compute it: the tick value belongs to the MASTER's
   // symbol on the MASTER's broker, and the copier never sees either.
   string riskJson = RiskJson(o);

   string json = StringFormat(
      "{\"ticket\":%d,\"symbol\":\"%s\",\"action\":\"%s\",\"direction\":\"%s\"," +
      "\"lots\":%.2f,\"price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,\"atr\":%.5f," +
      "\"magic\":%d,\"comment\":\"%s\",\"account\":\"%d\",%s\"ts\":%.3f}",
      o.ticket, o.symbol, action, direction,
      o.lots, o.price, o.sl, o.tp, atr,
      o.magic, JsonEscape(o.comment),
      AccountNumber(), riskJson, (double)TimeGMT());

   ZmqMsg msg(json);
   pubSocket.send(msg);
   Print(action, " ticket=", o.ticket, " -> ", json);
  }

//+------------------------------------------------------------------+
//| What the order stakes, and the account it stakes it against.       |
//|                                                                    |
//| Returns a JSON fragment ending in a comma, or an empty string when |
//| nothing can be said. Three fields, deliberately raw:               |
//|                                                                    |
//|   risk_amount   money lost if the stop is hit, in deposit currency |
//|   balance       AccountBalance() at the moment of the signal       |
//|   equity        AccountEquity() likewise                           |
//|                                                                    |
//| The FRACTION is not sent. It is risk_amount / balance and the hub  |
//| can divide; sending both the inputs and the result invites them to |
//| disagree, and then nobody knows which to believe.                  |
//|                                                                    |
//| risk_amount is omitted entirely when the order has no stop. A      |
//| position with no stop does not risk a known amount -- it risks     |
//| everything -- and sending 0 would read as "risks nothing", which   |
//| is the opposite. The hub treats an absent field as "not known".    |
//+------------------------------------------------------------------+
string RiskJson(OrderSnapshot &o)
  {
   string out = StringFormat("\"balance\":%.2f,\"equity\":%.2f,",
                             AccountBalance(), AccountEquity());

   if(o.sl <= 0.0)
      return out;   // no stop: the amount at risk is not a number

   double tickValue = MarketInfo(o.symbol, MODE_TICKVALUE);
   double tickSize  = MarketInfo(o.symbol, MODE_TICKSIZE);
   if(tickSize <= 0.0 || tickValue <= 0.0)
      return out;   // the terminal cannot price this symbol's tick

   double distance = MathAbs(o.price - o.sl);
   if(distance <= 0.0)
      return out;

   double risk = (distance / tickSize) * tickValue * o.lots;
   return out + StringFormat("\"risk_amount\":%.2f,", risk);
  }

//+------------------------------------------------------------------+
//| OrderComment() is broker-controlled text and can contain quotes or |
//| backslashes (brokers append things like "[sl]" or "to #123456"),   |
//| which would produce malformed JSON the hub silently drops.         |
//+------------------------------------------------------------------+
string JsonEscape(string raw)
  {
   string out = "";
   int n = StringLen(raw);
   for(int i = 0; i < n; i++)
     {
      ushort ch = StringGetCharacter(raw, i);
      if(ch == '"' || ch == '\\')
         out += "\\";
      else if(ch < 32)   // control chars: drop rather than emit raw
        {
         out += " ";
         continue;
        }
      out += ShortToString(ch);
     }
   return out;
  }

//+------------------------------------------------------------------+
void SendHeartbeat()
  {
   // The build each master is running, said by the master itself.
   //
   // Without this, the only way to tell two builds apart is to hash the
   // .ex4 files -- which is how an afternoon went: a bridge was deployed
   // to the wrong terminal, a fix was believed to be live that was not,
   // and three different binaries of one EA existed with nothing on any
   // screen to say so. At four to eight masters that stops being an
   // annoyance and becomes unanswerable.
   //
   // On the HEARTBEAT rather than the signal: it arrives every few
   // seconds whether or not anything trades, so the hub knows the version
   // of a master that has been quiet all session -- which is exactly the
   // one somebody is asking about.
   string json = StringFormat(
      "{\"account\":\"%d\",\"bridge_version\":\"%s\",\"ts\":%.3f}",
      AccountNumber(), BRIDGE_VERSION, (double)TimeGMT());
   ZmqMsg msg(json);
   pushSocket.send(msg);
  }
//+------------------------------------------------------------------+
