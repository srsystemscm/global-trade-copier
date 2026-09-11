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

   // LINGER=0 so OnDeinit can't block MT4's shutdown waiting to flush a
   // reply nobody is reading -- an unbounded linger shows up as
   // "shutdown by timeout" in the Experts log and hangs terminal close.
   repSocket.setLinger(0);

   if(!repSocket.bind(addr))
     {
      Print("SlaveBridge FAILED to bind ", addr,
            " -- another EA, or a stale mql-zmq context from a previous ",
            "attach, is still holding that port. Restart the terminal.");
      return(INIT_FAILED);
     }
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

   // Same as CLOSE: nothing to modify on a position that's already gone.
   // Without this, every later MODIFY the master sends for a hand-closed
   // slave leg logs an error on the hub for no actionable reason.
   if(OrderCloseTime() > 0)
      return StringFormat("{\"status\":\"ok\",\"already_closed\":true,\"slave_ticket\":%d}", ticket);

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

   // Already flat -- someone closed this leg by hand, or its SL/TP fired.
   // OrderSelect() searches history as well as open trades, so we find it,
   // and OrderClose() would reject it with 4108. That is the outcome CLOSE
   // was asking for, so report success: returning an error leaves the hub's
   // Trade Registry row stuck at OPEN forever, because MT4 mirror slaves
   // can't reconcile (MT4Adapter has no get_open_positions()).
   if(OrderCloseTime() > 0)
      return StringFormat("{\"status\":\"ok\",\"already_closed\":true,\"slave_ticket\":%d}", ticket);

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
//| isn't needed. It does have to tolerate whitespace, though: the    |
//| hub sends via Python's json.dumps(), which emits `"cmd": "PING"`  |
//| with a space after the colon. Matching on the literal `"cmd":"`   |
//| silently found nothing -- strings came back empty and every       |
//| number came back 0.0 (lots=0 => OrderSend rejected). scripts/     |
//| simulate_slave.py never caught it because json.loads() doesn't    |
//| care about whitespace; only this parser does.                     |
//+------------------------------------------------------------------+
int SkipWhitespace(string json, int i)
  {
   int len = StringLen(json);
   while(i < len)
     {
      ushort c = StringGetCharacter(json, i);
      if(c == ' ' || c == '	' || c == '
' || c == '')
         i++;
      else
         break;
     }
   return i;
  }

//+------------------------------------------------------------------+
//| Position just past `"<key>"` and its colon, or -1 if not present. |
//+------------------------------------------------------------------+
int FindValueStart(string json, string key)
  {
   string needle = "\"" + key + "\"";
   int p = StringFind(json, needle);
   if(p < 0)
      return -1;
   p = SkipWhitespace(json, p + StringLen(needle));
   if(p >= StringLen(json) || StringGetCharacter(json, p) != ':')
      return -1;
   return SkipWhitespace(json, p + 1);
  }

//+------------------------------------------------------------------+
string JsonGetString(string json, string key)
  {
   int p = FindValueStart(json, key);
   if(p < 0 || p >= StringLen(json))
      return "";
   if(StringGetCharacter(json, p) != '"')
      return "";
   p++;
   int end = StringFind(json, "\"", p);
   if(end < 0)
      return "";
   return StringSubstr(json, p, end - p);
  }

//+------------------------------------------------------------------+
double JsonGetDouble(string json, string key)
  {
   int p = FindValueStart(json, key);
   if(p < 0)
      return 0.0;
   int start = p;
   int len   = StringLen(json);
   while(p < len)
     {
      ushort c = StringGetCharacter(json, p);
      if((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E')
         p++;
      else
         break;
     }
   // no digits consumed => JSON null (the hub sends `"sl": null` when the
   // master has none), which MQL4 represents as 0.0 anyway.
   if(p == start)
      return 0.0;
   return StrToDouble(StringSubstr(json, start, p - start));
  }
//+------------------------------------------------------------------+
