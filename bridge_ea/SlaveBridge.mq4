//+------------------------------------------------------------------+
//| SlaveBridge.mq4                                                    |
//| Phase 2: slave-side bridge EA. Binds a ZMQ REP socket and executes |
//| OPEN / MODIFY / CLOSE commands sent by the hub's MT4Adapter,       |
//| replying with a JSON ACK (or error) for every command received.   |
//|                                                                    |
//| Requires the mql-zmq library (https://github.com/dingmaotu/mql-zmq)|
//| - Copy Zmq.mqh (and its dependencies) into MQL4/Include/Zmq/       |
//| - Copy libzmq.dll + libsodium.dll into MQL4/Libraries/              |
//| - Enable "Allow DLL imports" for this EA                           |
//+------------------------------------------------------------------+
#property strict

#include <Zmq/Zmq.mqh>

input int HubCommandPort = 5560;  // this slave's REP port -- must match the
                                    // "port" set for this slave in hub/config/slaves.json
input int PollIntervalMs = 100;    // how often to poll for a command
input int Slippage       = 5;

Context zmqContext("bridge_ea_slave");
Socket  repSocket(zmqContext, ZMQ_REP);

//+------------------------------------------------------------------+
int OnInit()
  {
   string addr = StringFormat("tcp://*:%d", HubCommandPort);
   repSocket.bind(addr);
   Print("SlaveBridge listening on ", addr);
   EventSetMillisecondTimer(PollIntervalMs);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   repSocket.unbind(StringFormat("tcp://*:%d", HubCommandPort));
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   ZmqMsg request;
   if(!repSocket.recv(request, true)) // non-blocking; nothing waiting
      return;

   string json  = request.getData();
   string reply = HandleCommand(json);

   ZmqMsg replyMsg(reply);
   repSocket.send(replyMsg);
  }

//+------------------------------------------------------------------+
string HandleCommand(string json)
  {
   string cmd = JsonGetString(json, "cmd");

   if(cmd == "PING")
      return "{\"status\":\"ok\"}";
   if(cmd == "OPEN")
      return HandleOpen(json);
   if(cmd == "MODIFY")
      return HandleModify(json);
   if(cmd == "CLOSE")
      return HandleClose(json);

   return StringFormat("{\"status\":\"error\",\"message\":\"unknown cmd '%s'\"}", cmd);
  }

//+------------------------------------------------------------------+
string HandleOpen(string json)
  {
   string symbol    = JsonGetString(json, "symbol");
   string direction = JsonGetString(json, "direction");
   double lots      = JsonGetDouble(json, "lots");
   double sl        = JsonGetDouble(json, "sl");
   double tp        = JsonGetDouble(json, "tp");

   int    type  = (direction == "BUY") ? OP_BUY : OP_SELL;
   double price = (type == OP_BUY) ? MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);

   int ticket = OrderSend(symbol, type, lots, price, Slippage, sl, tp, "copier", 0, 0, clrNONE);
   if(ticket < 0)
      return StringFormat("{\"status\":\"error\",\"message\":\"OrderSend failed: %d\"}", GetLastError());

   return StringFormat("{\"status\":\"ok\",\"slave_ticket\":%d,\"price\":%.5f}", ticket, price);
  }

//+------------------------------------------------------------------+
string HandleModify(string json)
  {
   int    ticket = (int)JsonGetDouble(json, "ticket");
   double sl     = JsonGetDouble(json, "sl");
   double tp     = JsonGetDouble(json, "tp");

   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return StringFormat("{\"status\":\"error\",\"message\":\"ticket %d not found\"}", ticket);

   if(!OrderModify(ticket, OrderOpenPrice(), sl, tp, 0, clrNONE))
      return StringFormat("{\"status\":\"error\",\"message\":\"OrderModify failed: %d\"}", GetLastError());

   return "{\"status\":\"ok\"}";
  }

//+------------------------------------------------------------------+
string HandleClose(string json)
  {
   int ticket = (int)JsonGetDouble(json, "ticket");

   if(!OrderSelect(ticket, SELECT_BY_TICKET))
      return StringFormat("{\"status\":\"error\",\"message\":\"ticket %d not found\"}", ticket);

   double lots   = OrderLots();
   int    type   = OrderType();
   string symbol = OrderSymbol();
   double price  = (type == OP_BUY) ? MarketInfo(symbol, MODE_BID) : MarketInfo(symbol, MODE_ASK);

   if(!OrderClose(ticket, lots, price, Slippage, clrNONE))
      return StringFormat("{\"status\":\"error\",\"message\":\"OrderClose failed: %d\"}", GetLastError());

   return "{\"status\":\"ok\"}";
  }

//+------------------------------------------------------------------+
//| Minimal flat-JSON value extraction -- every command here is a     |
//| single-level object with string/number values, so a full parser   |
//| isn't needed.                                                      |
//+------------------------------------------------------------------+
string JsonGetString(string json, string key)
  {
   string needle = "\"" + key + "\":\"";
   int start = StringFind(json, needle);
   if(start < 0)
      return "";
   start += StringLen(needle);
   int end = StringFind(json, "\"", start);
   if(end < 0)
      return "";
   return StringSubstr(json, start, end - start);
  }

//+------------------------------------------------------------------+
double JsonGetDouble(string json, string key)
  {
   string needle = "\"" + key + "\":";
   int start = StringFind(json, needle);
   if(start < 0)
      return 0.0;
   start += StringLen(needle);
   int end = start;
   int len = StringLen(json);
   while(end < len)
     {
      ushort c = StringGetCharacter(json, end);
      if((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.')
         end++;
      else
         break;
     }
   return StrToDouble(StringSubstr(json, start, end - start));
  }
//+------------------------------------------------------------------+
