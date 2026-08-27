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
//+------------------------------------------------------------------+
#property strict

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
  };

OrderSnapshot knownOrders[];
datetime lastHeartbeatSent = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   string pubAddr  = StringFormat("tcp://%s:%d", HubHost, HubPubPort);
   string pushAddr = StringFormat("tcp://%s:%d", HubHost, HubPushPort);

   pubSocket.connect(pubAddr);
   pushSocket.connect(pushAddr);

   Print("TradeCopierBridge connected: PUB->", pubAddr, "  PUSH->", pushAddr);

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
   knownOrders = current;

   if(TimeCurrent() - lastHeartbeatSent >= HeartbeatIntervalMs / 1000)
     {
      SendHeartbeat();
      lastHeartbeatSent = TimeCurrent();
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
     }
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
      "\"lots\":%.2f,\"price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,\"atr\":%.5f,\"account\":\"%d\",\"ts\":%.3f}",
      o.ticket, o.symbol, action, direction,
      o.lots, o.price, o.sl, o.tp, atr,
      AccountNumber(), TimeGMT());

   ZmqMsg msg(json);
   pubSocket.send(msg);
   Print(action, " ticket=", o.ticket, " -> ", json);
  }

//+------------------------------------------------------------------+
void SendHeartbeat()
  {
   string json = StringFormat("{\"account\":\"%d\",\"ts\":%.3f}", AccountNumber(), TimeGMT());
   ZmqMsg msg(json);
   pushSocket.send(msg);
  }
//+------------------------------------------------------------------+
