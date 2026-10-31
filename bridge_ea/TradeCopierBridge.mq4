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
//+------------------------------------------------------------------+
#property strict
#property version   "1.02"
#define   BRIDGE_VERSION "1.02"

#include <Zmq/Zmq.mqh>

input string HubHost              = "127.0.0.1";
input int    HubPubPort           = 5555;   // hub SUB socket - we PUB/connect here
input int    HubPushPort          = 5557;   // hub PULL socket - we PUSH/connect here
input int    PollIntervalMs       = 500;    // how often to diff open orders
input int    HeartbeatIntervalMs  = 5000;   // how often to send a heartbeat
input double SlTpEpsilon          = 0.00001; // ignore SL/TP deltas smaller than this (float noise)
input int    AtrPeriod            = 14;      // iATR() period used for the "atr" field
input int    AtrTimeframe         = PERIOD_CURRENT; // iATR() timeframe used for the "atr" field

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
      if(FindByTicket(curr, prev[i].ticket) < 0)
         PublishSignal("CLOSE", prev[i]);
     }
  }

//+------------------------------------------------------------------+
void PublishSignal(string action, OrderSnapshot &o)
  {
   string direction = (o.type == OP_BUY) ? "BUY" : "SELL";
   double atr = iATR(o.symbol, AtrTimeframe, AtrPeriod, 0);
   string json = StringFormat(
      "{\"ticket\":%d,\"symbol\":\"%s\",\"action\":\"%s\",\"direction\":\"%s\"," +
      "\"lots\":%.2f,\"price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,\"atr\":%.5f," +
      "\"magic\":%d,\"comment\":\"%s\",\"account\":\"%d\",\"ts\":%.3f}",
      o.ticket, o.symbol, action, direction,
      o.lots, o.price, o.sl, o.tp, atr,
      o.magic, JsonEscape(o.comment),
      AccountNumber(), (double)TimeGMT());

   ZmqMsg msg(json);
   pubSocket.send(msg);
   Print(action, " ticket=", o.ticket, " -> ", json);
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
   string json = StringFormat("{\"account\":\"%d\",\"ts\":%.3f}", AccountNumber(), (double)TimeGMT());
   ZmqMsg msg(json);
   pushSocket.send(msg);
  }
//+------------------------------------------------------------------+
