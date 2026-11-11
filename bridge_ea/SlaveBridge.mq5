//+------------------------------------------------------------------+
//| SlaveBridge.mq5                                                  |
//| MT5 port of SlaveBridge.mq4 1.03. Binds a ZMQ REP socket and     |
//| executes the same OPEN / MODIFY / CLOSE / PING / POSITIONS       |
//| commands over the same JSON wire format, so the hub drives an    |
//| MT5 slave with no hub-side changes at all -- the slave still     |
//| registers as broker_type "mt4" in hub/config/slaves.json,        |
//| because MT4Adapter is pure JSON-over-ZMQ REQ/REP and never       |
//| touches anything MT4-specific.                                   |
//|                                                                  |
//| This is NOT a transliteration. MQL5 has none of OrderSend(),     |
//| OrderClose(), OrderModify(), OrderSelect(), MarketInfo(),        |
//| OrdersTotal() or OrderProfit(), so every handler is rebuilt      |
//| around MqlTradeRequest and the position API. The places where a  |
//| literal port would be silently wrong are marked PORT NOTE.       |
//|                                                                  |
//| Requires mql-zmq (https://github.com/dingmaotu/mql-zmq):         |
//|   Include/Zmq/*.mqh       -> MQL5/Include/Zmq/                   |
//|   Include/Mql/Lang/*.mqh  -> MQL5/Include/Mql/Lang/              |
//|   Library/MT5 libzmq.dll + libsodium.dll -> MQL5/Libraries/      |
//|     (MT5 is 64-bit: the MT4 DLLs are 32-bit and will not load)   |
//|   Tools > Options > Expert Advisors > Allow DLL imports          |
//|   AutoTrading on, or every trade fails with 10027 while          |
//|   everything else looks healthy.                                 |
//|                                                                  |
//| Versions (bump both lines below with every change):              |
//|   1.00 2026-10-31 port of SlaveBridge.mq4 1.03                   |
//|   1.01 2026-11-11 POSITIONS reports each leg's SL/TP            |
//+------------------------------------------------------------------+
#property version   "1.01"
#define   BRIDGE_VERSION "1.01"

#include <Zmq/Zmq.mqh>

input int  HubCommandPort       = 5560;  // this slave's REP port -- must match the
                                         // "port" set for this slave in hub/config/slaves.json
input int  PollIntervalMs       = 100;   // how often to poll for a command
input int  Slippage             = 5;     // deviation (points) allowed on market orders
input long MagicNumber          = 0;     // stamped on every copier position
input bool AllowNettingAccount  = false; // see the OnInit guard -- read it before setting true

Context zmqContext("bridge_ea_slave");
Socket  repSocket(zmqContext, ZMQ_REP);

//+------------------------------------------------------------------+
int OnInit()
  {
   //--- PORT NOTE: the one guard with no MT4 counterpart.
   // MT4 accounts are always hedging: every OrderSend makes its own
   // independent ticket. An MT5 account can be NETTING instead, where one
   // symbol holds at most one position -- a second BUY merges into it and a
   // SELL reduces it. That breaks the copier's model outright, not
   // cosmetically: the Trade Registry is keyed UNIQUE(slave_id,
   // master_ticket, slave_symbol) and get_open_mappings() hands the engine
   // exactly one slave_ticket per master ticket. On a netting account two
   // master tickets in the same symbol collapse onto one position ticket, so
   // closing the first master ticket closes the second master's volume too,
   // and an opposite-direction signal silently nets an existing leg away.
   // Fail loudly here rather than discover it mid-trade.
   ENUM_ACCOUNT_MARGIN_MODE margin =
      (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   if(margin != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
     {
      if(!AllowNettingAccount)
        {
         Print("SlaveBridge REFUSING to start: this account is not in hedging mode ",
               "(ACCOUNT_MARGIN_MODE=", EnumToString(margin), "). The copier maps one ",
               "master ticket to one slave position ticket, which a netting account ",
               "cannot honour -- positions in the same symbol merge, so a CLOSE for one ",
               "master ticket closes another master's volume as well. Use a hedging MT5 ",
               "account, or set AllowNettingAccount=true if you accept that.");
         return(INIT_FAILED);
        }
      Print("SlaveBridge WARNING: netting account accepted by input override. ",
            "Any two open master tickets in the same symbol will share one position, ",
            "and CLOSE/MODIFY for one will hit the other. Only safe with a single ",
            "open position per symbol at a time.");
     }

   // Warn, don't fail: these are checkbox state the operator can flip after
   // attaching, and failing would make them re-attach for no reason.
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      Print("SlaveBridge WARNING: AutoTrading is OFF -- commands will be accepted ",
            "and every trade will fail with 10027.");
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("SlaveBridge WARNING: 'Allow Algo Trading' is off for this EA -- ",
            "every trade will fail with 10027.");

   string addr = StringFormat("tcp://*:%d", HubCommandPort);

   // LINGER=0 so OnDeinit can't block the terminal's shutdown waiting to
   // flush a reply nobody is reading -- an unbounded linger shows up as
   // "shutdown by timeout" in the Experts log and hangs terminal close.
   repSocket.setLinger(0);

   if(!repSocket.bind(addr))
     {
      Print("SlaveBridge FAILED to bind ", addr,
            " -- another EA, or a stale mql-zmq context from a previous ",
            "attach, is still holding that port. Restart the terminal.");
      return(INIT_FAILED);
     }

   Print("SlaveBridge.mq5 ", BRIDGE_VERSION, " listening on ", addr,
         "  account=", AccountInfoInteger(ACCOUNT_LOGIN),
         " mode=", EnumToString(margin));
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
   if(cmd == "POSITIONS")
      return HandlePositions();

   return JsonError("unknown cmd '" + cmd + "'");
  }

//+------------------------------------------------------------------+
string HandleOpen(string json)
  {
   string symbol    = JsonGetString(json, "symbol");
   string direction = JsonGetString(json, "direction");
   double lots      = JsonGetDouble(json, "lots");
   double sl        = JsonGetDouble(json, "sl");
   double tp        = JsonGetDouble(json, "tp");

   //--- PORT NOTE: mirror mode passes the MASTER's symbol string through
   // untranslated (hub/app/copy_engine.py). Two terminals at the same broker
   // always agreed; an MT4 master and an MT5 slave at different brokers
   // usually do not (XAUUSD vs XAUUSD.r vs GOLD). Say so precisely, because
   // the bare retcode for this is indistinguishable from a dozen others.
   if(!SymbolSelect(symbol, true))
      return JsonError(StringFormat(
         "symbol '%s' is not offered by this broker (or could not be added to "
         "Market Watch) -- mirror mode sends the master's symbol name verbatim",
         symbol));

   if(!AwaitPrice(symbol))
      return JsonError(StringFormat(
         "no price for '%s' yet -- it was just added to Market Watch and has "
         "not ticked", symbol));

   bool   isBuy = (direction == "BUY");
   double price = isBuy ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                        : SymbolInfoDouble(symbol, SYMBOL_BID);

   string volErr = "";
   double volume = NormalizeVolume(symbol, lots, volErr);
   if(volErr != "")
      return JsonError(volErr);

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = symbol;
   req.volume       = volume;
   req.type         = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price        = NormalizeDouble(price, digits);
   // A master SL/TP of 0 means "unset", exactly as in MQL4 -- and 0 is also
   // how MQL5 says "no level", so this passes straight through.
   req.sl           = (sl > 0.0) ? NormalizeDouble(sl, digits) : 0.0;
   req.tp           = (tp > 0.0) ? NormalizeDouble(tp, digits) : 0.0;
   req.deviation    = (ulong)Slippage;
   req.magic        = (ulong)MagicNumber;
   req.comment      = "copier";
   req.type_filling = PickFilling(symbol);

   string stopsErr = CheckStopsDistance(symbol, req.type, req.price, req.sl, req.tp);
   if(stopsErr != "")
      return JsonError(stopsErr);

   if(!OrderSend(req, res))
      return JsonError(StringFormat("OrderSend failed to submit: retcode=%d %s (last_error=%d)",
                                    (int)res.retcode, res.comment, GetLastError()));

   if(res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL)
      return JsonError(StringFormat("OrderSend rejected: retcode=%d %s",
                                    (int)res.retcode, res.comment));

   if(res.retcode == TRADE_RETCODE_DONE_PARTIAL)
      Print("SlaveBridge WARNING: ", symbol, " filled only ", res.volume, " of ",
            volume, " -- the hub will track the position at the filled size.");

   //--- PORT NOTE: MqlTradeResult has no position ticket, and the hub needs
   // one: every later MODIFY/CLOSE arrives keyed by whatever we return here.
   // On a hedging account the position ticket equals the opening order's
   // ticket, but the position can take a moment to appear, and on a netting
   // account it is the pre-existing position's id instead -- so resolve it
   // properly rather than assuming res.order.
   ulong posTicket = ResolvePositionTicket(res.order, res.deal);
   if(posTicket == 0)
      return JsonError(StringFormat(
         "order %s filled but its position ticket could not be resolved -- the "
         "position is OPEN AND UNTRACKED, close it by hand",
         IntegerToString((long)res.order)));

   double fillPrice = (res.price > 0.0) ? res.price : req.price;

   return "{\"status\":\"ok\",\"slave_ticket\":" + IntegerToString((long)posTicket)
        + ",\"price\":" + DoubleToString(fillPrice, digits)
        + ",\"lots\":" + DoubleToString((res.volume > 0.0) ? res.volume : volume, 2)
        + "}";
  }

//+------------------------------------------------------------------+
string HandleModify(string json)
  {
   ulong  ticket = JsonGetTicket(json, "ticket");
   double sl     = JsonGetDouble(json, "sl");
   double tp     = JsonGetDouble(json, "tp");

   if(!PositionSelectByTicket(ticket))
     {
      //--- PORT NOTE: this is the trap in the whole port. In MQL4,
      // OrderSelect(SELECT_BY_TICKET) searches history too, so a hand-closed
      // leg was found and OrderCloseTime()>0 identified it -- which is how
      // 1.02 reports an already-closed leg as success. In MQL5 a closed
      // position is simply absent from PositionSelectByTicket, so without
      // this history fallback every hand-closed leg comes back as an error
      // and strands its Trade Registry row at OPEN forever. That was a real
      // bug on 2026-09-10, not a hypothetical.
      if(PositionExistedAndClosed(ticket))
         return "{\"status\":\"ok\",\"already_closed\":true,\"slave_ticket\":"
              + IntegerToString((long)ticket) + "}";
      return JsonError("ticket " + IntegerToString((long)ticket) + " not found");
     }

   string symbol = PositionGetString(POSITION_SYMBOL);
   int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action   = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.symbol   = symbol;
   req.sl       = (sl > 0.0) ? NormalizeDouble(sl, digits) : 0.0;
   req.tp       = (tp > 0.0) ? NormalizeDouble(tp, digits) : 0.0;

   ENUM_ORDER_TYPE asOrder = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
                             ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   string stopsErr = CheckStopsDistance(symbol, asOrder,
                                        PositionGetDouble(POSITION_PRICE_CURRENT),
                                        req.sl, req.tp);
   if(stopsErr != "")
      return JsonError(stopsErr);

   if(!OrderSend(req, res))
      return JsonError(StringFormat("SLTP failed to submit: retcode=%d %s (last_error=%d)",
                                    (int)res.retcode, res.comment, GetLastError()));

   // 10025 means the levels already match what was asked for. MQL4's
   // OrderModify returned false with error 1 for the same case; either way it
   // is the outcome MODIFY wanted, so it is not an error.
   if(res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_NO_CHANGES)
      return JsonError(StringFormat("SLTP rejected: retcode=%d %s",
                                    (int)res.retcode, res.comment));

   return "{\"status\":\"ok\"}";
  }

//+------------------------------------------------------------------+
string HandleClose(string json)
  {
   ulong ticket = JsonGetTicket(json, "ticket");

   if(!PositionSelectByTicket(ticket))
     {
      // Already flat -- closed by hand, or its SL/TP fired before the
      // master's CLOSE arrived. That is the outcome CLOSE was asking for, so
      // report success: an error here leaves the registry row stuck at OPEN.
      // See the PORT NOTE in HandleModify for why this needs history.
      if(PositionExistedAndClosed(ticket))
         return "{\"status\":\"ok\",\"already_closed\":true,\"slave_ticket\":"
              + IntegerToString((long)ticket) + "}";
      return JsonError("ticket " + IntegerToString((long)ticket) + " not found");
     }

   string symbol = PositionGetString(POSITION_SYMBOL);
   double volume = PositionGetDouble(POSITION_VOLUME);
   bool   isLong = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   //--- PORT NOTE: MQL5 has no OrderClose(). A position is closed by sending
   // an opposing deal that names it in `position` -- without that field the
   // deal would open a second, hedged position instead of closing this one.
   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action       = TRADE_ACTION_DEAL;
   req.position     = ticket;
   req.symbol       = symbol;
   req.volume       = volume;
   req.type         = isLong ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   req.price        = NormalizeDouble(isLong ? SymbolInfoDouble(symbol, SYMBOL_BID)
                                             : SymbolInfoDouble(symbol, SYMBOL_ASK), digits);
   req.deviation    = (ulong)Slippage;
   req.magic        = (ulong)MagicNumber;
   req.type_filling = PickFilling(symbol);

   if(!OrderSend(req, res))
      return JsonError(StringFormat("close failed to submit: retcode=%d %s (last_error=%d)",
                                    (int)res.retcode, res.comment, GetLastError()));

   if(res.retcode == TRADE_RETCODE_DONE_PARTIAL)
     {
      // Deliberately an error. The hub marks the registry row CLOSED on
      // success, which would abandon the remaining volume with nothing
      // tracking it. Reporting failure keeps the row OPEN and puts the
      // leftover in front of a human.
      double left = PositionSelectByTicket(ticket) ? PositionGetDouble(POSITION_VOLUME) : 0.0;
      return JsonError(StringFormat(
         "close only partially filled: %.2f of %.2f lots remain on ticket %s",
         left, volume, IntegerToString((long)ticket)));
     }

   if(res.retcode != TRADE_RETCODE_DONE)
      return JsonError(StringFormat("close rejected: retcode=%d %s",
                                    (int)res.retcode, res.comment));

   return "{\"status\":\"ok\"}";
  }

//+------------------------------------------------------------------+
//| Reports every open position on this account, so the hub's         |
//| MT4Adapter can implement get_open_positions(). Two things depend  |
//| on it: Trade Registry reconciliation, and the dashboard's live    |
//| P&L.                                                              |
//|                                                                   |
//| Reports ALL positions, not just copier-placed ones: reconciliation|
//| specifically needs to see untracked positions so it can warn about|
//| them rather than assume they're ours to touch. That is why this   |
//| does not filter on MagicNumber.                                   |
//+------------------------------------------------------------------+
string HandlePositions()
  {
   string items = "";
   int    n     = 0;

   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong ticket = PositionGetTicket(i);   // also selects it
      if(ticket == 0)
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      double lots   = PositionGetDouble(POSITION_VOLUME);
      bool   isLong = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

      if(n > 0)
         items += ",";

      // Signed quantity, matching the hub's Position.quantity convention.
      double qty = isLong ? lots : -lots;

      //--- PORT NOTE: MQL4 had OrderCommission() on the order itself. In MQL5
      // commission lives on the deals, not the position, so including it
      // would mean a HistorySelectByPosition() per position on a read the
      // dashboard polls every 5s. Profit + swap it is; the figure is a few
      // cents off the realisable amount rather than wrong.
      double pnl = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);

      items += "{\"ticket\":" + IntegerToString((long)ticket)
             + ",\"symbol\":\"" + symbol + "\""
             + ",\"quantity\":" + DoubleToString(qty, 2)
             + ",\"average_price\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), digits)
             // Reported so the hub can confirm a MODIFY landed; 0 means "not
             // set", which the hub reads as None rather than a stop at zero.
             + ",\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), digits)
             + ",\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), digits)
             + ",\"unrealized_pnl\":" + DoubleToString(pnl, 2)
             + "}";
      n++;
     }

   return "{\"status\":\"ok\",\"positions\":[" + items + "]}";
  }

//+------------------------------------------------------------------+
//| Trade helpers                                                     |
//+------------------------------------------------------------------+

//| The filling mode a symbol actually permits. Getting this wrong is  |
//| retcode 10030 and nothing fills -- there is no MT4 equivalent to   |
//| forget, which is exactly why it gets forgotten. FOK is preferred:  |
//| the copier maps one master ticket to one slave position, so an     |
//| all-or-nothing fill is worth more here than a partial one.         |
ENUM_ORDER_TYPE_FILLING PickFilling(const string symbol)
  {
   long mask = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((mask & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   if((mask & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
  }

//| Rounds to the symbol's volume step, and REFUSES anything outside   |
//| its min/max rather than clamping. Clamping is the tempting choice  |
//| and the wrong one: a master 0.01 against a slave minimum of 0.10   |
//| would silently trade ten times the intended risk. Mirror mode      |
//| copies lots 1:1 with no scaling, so the two brokers' contract      |
//| sizes have to agree for the copy to mean anything anyway.          |
double NormalizeVolume(const string symbol, double lots, string &err)
  {
   double vmin = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      step = 0.01;

   double v = MathRound(lots / step) * step;
   v = NormalizeDouble(v, VolumeDigits(step));

   if(MathAbs(v - lots) > 1e-8)
      Print("SlaveBridge: ", symbol, " volume ", lots, " rounded to ", v,
            " (step ", step, ")");

   if(v < vmin - 1e-8)
     {
      err = StringFormat("lots %.4f is below %s minimum %.4f -- refusing rather than "
                         "rounding up, which would multiply the intended risk",
                         lots, symbol, vmin);
      return 0.0;
     }
   if(vmax > 0.0 && v > vmax + 1e-8)
     {
      err = StringFormat("lots %.4f exceeds %s maximum %.4f -- this EA does not split "
                         "an order across deals", lots, symbol, vmax);
      return 0.0;
     }
   return v;
  }

int VolumeDigits(double step)
  {
   for(int d = 0; d < 8; d++)
     {
      double scaled = step * MathPow(10, d);
      if(MathAbs(scaled - MathRound(scaled)) < 1e-9)
         return d;
     }
   return 2;
  }

//| A stop closer to market than SYMBOL_TRADE_STOPS_LEVEL is rejected  |
//| with 10016, whose bare retcode says nothing useful. Name the real  |
//| distance instead -- and do NOT move the level to fit, because the  |
//| master chose it and quietly widening a stop changes the risk.      |
string CheckStopsDistance(const string symbol, ENUM_ORDER_TYPE type,
                          double price, double sl, double tp)
  {
   long stopsPoints = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stopsPoints <= 0 || price <= 0.0)
      return "";

   double point   = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double minDist = stopsPoints * point;
   bool   isBuy   = (type == ORDER_TYPE_BUY);

   if(sl > 0.0)
     {
      double d = isBuy ? (price - sl) : (sl - price);
      if(d < minDist)
         return StringFormat("SL %.5f is %.5f from price %.5f, inside %s's minimum stop "
                             "distance of %d points (%.5f)",
                             sl, d, price, symbol, (int)stopsPoints, minDist);
     }
   if(tp > 0.0)
     {
      double d = isBuy ? (tp - price) : (price - tp);
      if(d < minDist)
         return StringFormat("TP %.5f is %.5f from price %.5f, inside %s's minimum stop "
                             "distance of %d points (%.5f)",
                             tp, d, price, symbol, (int)stopsPoints, minDist);
     }
   return "";
  }

//| The position a just-filled order produced. On hedging the position |
//| ticket is the opening order's ticket, but it can lag the OrderSend |
//| return by a moment, so wait briefly before falling back to the     |
//| deal's position id (which is also the netting answer).             |
ulong ResolvePositionTicket(ulong orderTicket, ulong dealTicket)
  {
   for(int attempt = 0; attempt < 20; attempt++)   // ~1s, well inside the hub's 5s timeout
     {
      if(orderTicket > 0 && PositionSelectByTicket(orderTicket))
         return orderTicket;

      if(dealTicket > 0 && HistoryDealSelect(dealTicket))
        {
         long posId = HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
         if(posId > 0 && PositionSelectByTicket((ulong)posId))
            return (ulong)posId;
        }
      Sleep(50);
     }
   return 0;
  }

//| True when this ticket is a position that USED to exist and is now   |
//| closed, as opposed to one that never existed. Only meaningful after |
//| PositionSelectByTicket() has already failed.                        |
bool PositionExistedAndClosed(ulong ticket)
  {
   if(!HistorySelectByPosition((long)ticket))
      return false;
   return (HistoryDealsTotal() > 0);
  }

//| A symbol added to Market Watch this instant has no price until it   |
//| ticks, and a 0 price would otherwise be sent as the order price.    |
bool AwaitPrice(const string symbol)
  {
   for(int attempt = 0; attempt < 20; attempt++)   // ~1s
     {
      if(SymbolInfoDouble(symbol, SYMBOL_BID) > 0.0 &&
         SymbolInfoDouble(symbol, SYMBOL_ASK) > 0.0)
         return true;
      Sleep(50);
     }
   return false;
  }

//+------------------------------------------------------------------+
//| JSON                                                              |
//|                                                                   |
//| Ported from SlaveBridge.mq4 unchanged apart from StrToDouble ->    |
//| StringToDouble and real escape sequences for the whitespace chars. |
//| Minimal flat-value extraction: every command is a single-level     |
//| object with string/number values, so a full parser isn't needed.   |
//| It does have to tolerate whitespace, though -- the hub sends via   |
//| Python's json.dumps(), and matching on a literal "cmd":" once      |
//| silently found nothing, making every number 0.0 (lots=0 => order   |
//| rejected). simulate_slave.py never caught it because json.loads()  |
//| doesn't care about whitespace; only this parser does.              |
//+------------------------------------------------------------------+

//| Error replies carry broker comments and symbol names, either of    |
//| which can contain a quote or a backslash and break the hub's       |
//| json.loads(). Neutralise both rather than lose the diagnostic.     |
string JsonError(string message)
  {
   StringReplace(message, "\\", "/");
   StringReplace(message, "\"", "'");
   return "{\"status\":\"error\",\"message\":\"" + message + "\"}";
  }

int SkipWhitespace(string json, int i)
  {
   int len = StringLen(json);
   while(i < len)
     {
      ushort c = StringGetCharacter(json, i);
      if(c == ' ' || c == '\t' || c == '\n' || c == '\r')
         i++;
      else
         break;
     }
   return i;
  }

//| Position just past the key and its colon, or -1 if not present.    |
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

//| The raw numeric token for a key, or "" when absent or JSON null.   |
string JsonGetNumberToken(string json, string key)
  {
   int p = FindValueStart(json, key);
   if(p < 0)
      return "";
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
   if(p == start)
      return "";   // JSON null -- the hub sends "sl": null for an unset level
   return StringSubstr(json, start, p - start);
  }

double JsonGetDouble(string json, string key)
  {
   string tok = JsonGetNumberToken(json, key);
   if(tok == "")
      return 0.0;   // null, which both MQL4 and MQL5 represent as 0.0 anyway
   return StringToDouble(tok);
  }

//| PORT NOTE: MT5 tickets are ulong and run far larger than MT4's int |
//| ones. Parsing them as a double then casting would be exact up to   |
//| 2^53 and so fine in practice, but going through StringToInteger    |
//| keeps it exact by construction rather than by argument.            |
ulong JsonGetTicket(string json, string key)
  {
   string tok = JsonGetNumberToken(json, key);
   if(tok == "")
      return 0;
   return (ulong)StringToInteger(tok);
  }
//+------------------------------------------------------------------+
