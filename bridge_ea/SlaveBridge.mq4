//+------------------------------------------------------------------+
//| SlaveBridge.mq4                                                    |
//| Slave-side bridge EA. Binds a ZMQ REP socket and executes the       |
//| commands the hub's MT4Adapter sends, replying with a JSON ACK (or   |
//| error) for every one:                                              |
//|                                                                    |
//|   OPEN MODIFY CLOSE POSITIONS   trading and reconciliation         |
//|   ACCOUNT SYMBOLS MARGIN        risk-based sizing                  |
//|   BARS                          autonomous mode (this slave's ATR) |
//|                                                                    |
//| Requires the mql-zmq library (https://github.com/dingmaotu/mql-zmq)|
//| - Copy Zmq.mqh (and its dependencies) into MQL4/Include/Zmq/       |
//| - Copy libzmq.dll + libsodium.dll into MQL4/Libraries/              |
//| - Enable "Allow DLL imports" for this EA                           |
//|                                                                    |
//| Versions (bump both lines below with every change):                |
//|   1.00 2026-08-26 initial build                                    |
//|   1.01 2026-09-10 fixes from the first real terminal shakeout      |
//|   1.02 2026-09-10 an already-closed leg counts as success          |
//|   1.03 2026-10-16 POSITIONS command (reconciliation, live P&L)     |
//|   1.04 2026-11-11 POSITIONS reports each leg's SL/TP              |
//|   1.05 2026-11-11 report actual fill price + lots on OPEN/CLOSE   |
//|   1.07 2026-11-15 ACCOUNT, SYMBOLS, MARGIN and BARS ported from   |
//|                   SlaveBridge.mq5 -- risk sizing and autonomous   |
//|                   mode now work on an MT4 slave, not just MT5.     |
//|                   1.06 is SKIPPED: a version number has to mean    |
//|                   the same capability on both bridges, because the |
//|                   hub's recompile hint maps commands to versions.   |
//+------------------------------------------------------------------+
#property strict
#property version   "1.07"
#define   BRIDGE_VERSION "1.07"

#include <Zmq/Zmq.mqh>

input int HubCommandPort = 5560;  // this slave's REP port -- must match the
                                    // "port" set for this slave in hub/config/slaves.json
input int PollIntervalMs = 100;    // how often to poll for a command
input int Slippage       = 5;
input int SymbolsReplyMax = 200;  // most symbols one SYMBOLS reply may carry,
                                    // for the "match" and Market Watch forms
input int BarsReplyMax    = 400;  // most bars one BARS reply may carry. An ATR
                                    // wants period+5, so this covers any period
                                    // worth using while keeping the reply inside
                                    // one socket message

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
   Print("SlaveBridge ", BRIDGE_VERSION, " listening on ", addr);
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
   if(cmd == "ACCOUNT")
      return HandleAccount();
   if(cmd == "SYMBOLS")
      return HandleSymbols(json);
   if(cmd == "MARGIN")
      return HandleMargin(json);
   if(cmd == "BARS")
      return HandleBars(json);

   // The hub keys on this phrase: _ask_tolerating_old_ea turns "unknown
   // cmd" into NotImplementedError so a caller can degrade, which is how a
   // hub upgraded ahead of the EAs on someone's desktop stays usable.
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

   int    type  = (direction == "BUY") ? OP_BUY : OP_SELL;
   double price = (type == OP_BUY) ? MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);

   int ticket = OrderSend(symbol, type, lots, price, Slippage, sl, tp, "copier", 0, 0, clrNONE);
   if(ticket < 0)
      return StringFormat("{\"status\":\"error\",\"message\":\"OrderSend failed: %d\"}", GetLastError());

   // Report what actually filled, not what was asked for. The gap between
   // the two IS the slippage this copy cost, and the broker states it once
   // -- it cannot be reconstructed from anything the hub keeps.
   double filled     = price;
   double filledLots = lots;
   if(OrderSelect(ticket, SELECT_BY_TICKET))
     {
      filled     = OrderOpenPrice();
      filledLots = OrderLots();
     }

   return StringFormat("{\"status\":\"ok\",\"slave_ticket\":%d,\"price\":%.5f,\"lots\":%.2f}",
                       ticket, filled, filledLots);
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

   // Same reasoning as OPEN: the exit fill is the other half of the round
   // trip, and without it only half the copy's variance can be measured.
   double closed = price;
   if(OrderSelect(ticket, SELECT_BY_TICKET))
      closed = OrderClosePrice();

   return StringFormat("{\"status\":\"ok\",\"price\":%.5f}", closed);
  }

//+------------------------------------------------------------------+
//| Reports every open market position on this account, so the hub's   |
//| MT4Adapter can implement get_open_positions(). Two things depend   |
//| on it: Trade Registry reconciliation (which was skipped entirely   |
//| for MT4 slaves, meaning a row that drifted from the account could  |
//| never be corrected), and the dashboard's live P&L for MT4 slaves.  |
//|                                                                     |
//| Reports ALL positions, not just copier-placed ones: reconciliation  |
//| specifically needs to see untracked positions so it can warn about  |
//| them rather than assume they're ours to touch.                      |
//+------------------------------------------------------------------+
string HandlePositions()
  {
   string items = "";
   int    n     = 0;

   for(int i = 0; i < OrdersTotal(); i++)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      if(OrderType() != OP_BUY && OrderType() != OP_SELL)
         continue;   // pending orders aren't positions

      if(n > 0)
         items += ",";

      // Signed quantity, matching the hub's Position.quantity convention.
      double qty = (OrderType() == OP_BUY) ? OrderLots() : -OrderLots();
      // Net floating P&L: gross profit plus the carrying costs, so the
      // dashboard shows what the account would actually realise.
      double pnl = OrderProfit() + OrderSwap() + OrderCommission();

      // SL/TP are reported so the hub can confirm a MODIFY actually landed:
      // nothing else reads a leg's protective levels back off the account.
      // 0 means "not set" here exactly as it does everywhere else in MQL4,
      // and the hub turns it into None rather than a stop at price zero.
      items += StringFormat(
         "{\"ticket\":%d,\"symbol\":\"%s\",\"quantity\":%.2f,\"average_price\":%.5f," +
         "\"sl\":%.5f,\"tp\":%.5f,\"unrealized_pnl\":%.2f}",
         OrderTicket(), OrderSymbol(), qty, OrderOpenPrice(),
         OrderStopLoss(), OrderTakeProfit(), pnl);
      n++;
     }

   return StringFormat("{\"status\":\"ok\",\"positions\":[%s]}", items);
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
//| ACCOUNT -- what this terminal's account is worth.                  |
//|                                                                    |
//| What lets a mirror slave size by a percentage of its OWN capital    |
//| instead of a multiple of the master's lots. Without it the bridge   |
//| cannot say what the account is worth, which is the whole reason     |
//| mirror mode once only had risk_scale.                              |
//|                                                                    |
//| PORT NOTE: the classic MQL4 Account* functions rather than          |
//| AccountInfoDouble/Integer. Both exist on build 600+, but these are  |
//| in every MT4 build, and this EA gets deployed by hand into          |
//| terminals nobody has audited.                                      |
//|                                                                    |
//| The hub reads login, balance and equity. The rest is for a human    |
//| reading a reply when something looks wrong.                        |
//+------------------------------------------------------------------+
string HandleAccount()
  {
   double margin = AccountMargin();
   // MT4 has no ACCOUNT_MARGIN_LEVEL. Equity/margin is what the terminal
   // itself shows, and 0 when flat rather than a division by zero.
   double level  = (margin > 0.0) ? (AccountEquity() / margin * 100.0) : 0.0;

   return "{\"status\":\"ok\""
        + ",\"login\":" + IntegerToString(AccountNumber())
        + ",\"server\":" + JsonQuote(AccountServer())
        + ",\"company\":" + JsonQuote(AccountCompany())
        // MT4 is ALWAYS hedging -- this platform has no netting account.
        // Stated rather than omitted, because the hub's netting checks
        // read very differently on the two and a blank would leave that
        // to be assumed.
        + ",\"margin_mode\":\"RETAIL_HEDGING\""
        + ",\"trade_mode\":" + JsonQuote(IsDemo() ? "DEMO" : "REAL")
        + ",\"currency\":" + JsonQuote(AccountCurrency())
        + ",\"balance\":" + DoubleToString(AccountBalance(), 2)
        + ",\"equity\":" + DoubleToString(AccountEquity(), 2)
        + ",\"margin\":" + DoubleToString(margin, 2)
        + ",\"margin_free\":" + DoubleToString(AccountFreeMargin(), 2)
        + ",\"margin_level\":" + DoubleToString(level, 2)
        + ",\"leverage\":" + IntegerToString(AccountLeverage())
        // Two separate switches, and an order fails on either. Reported
        // apart because "trading is off" has two different fixes.
        + ",\"trade_allowed\":" + (IsTradeAllowed() ? "true" : "false")
        + ",\"experts_enabled\":" + (IsExpertEnabled() ? "true" : "false")
        + ",\"bridge_version\":" + JsonQuote(BRIDGE_VERSION)
        + "}";
  }

//+------------------------------------------------------------------+
//| SYMBOLS -- specification and price, for sizing.                    |
//|                                                                    |
//| Risk-based sizing needs to know what one lot of the target IS.      |
//| Tick value over tick size is the only trustworthy way to ask what a |
//| point is worth, because brokers disagree on every other expression  |
//| of it.                                                             |
//+------------------------------------------------------------------+
string HandleSymbols(string json)
  {
   string names[];
   int    n         = JsonGetStringArray(json, "symbols", names);
   bool   named     = (n > 0);
   string match     = JsonGetString(json, "match");
   bool   namesOnly = JsonGetBool(json, "names_only");
   bool   truncated = false;

   int limit = (int)JsonGetDouble(json, "limit");
   if(limit <= 0 || limit > SymbolsReplyMax)
      limit = SymbolsReplyMax;

   if(n == 0)
     {
      bool   watchOnly = (match == "");
      string needle    = match;
      StringToUpper(needle);

      int total = SymbolsTotal(watchOnly);
      for(int i = 0; i < total; i++)
        {
         string s = SymbolName(i, watchOnly);
         if(s == "")
            continue;
         // "*" is the whole tree; an empty match already means Market
         // Watch, so there is no other way to ask for everything.
         if(!watchOnly && needle != "*")
           {
            string hay = s;
            StringToUpper(hay);
            if(StringFind(hay, needle) < 0)
               continue;
           }
         if(n >= limit)
           {
            truncated = true;   // reported, so a short list is never
            break;              // mistaken for a complete one
           }
         ArrayResize(names, n + 1);
         names[n] = s;
         n++;
        }
     }

   string items = "";
   for(int i = 0; i < n; i++)
     {
      if(i > 0)
         items += ",";
      // Wait for a price only when the caller NAMED its symbols. A named
      // read is a sizing enquiry and a spec without a price is no use to
      // it; a Market Watch scan must not wait, or two hundred symbols at
      // a second each would stall OnTimer past the hub's timeout.
      items += namesOnly ? JsonQuote(names[i]) : SymbolSpecJson(names[i], named);
     }

   return "{\"status\":\"ok\",\"count\":" + IntegerToString(n)
        + ",\"truncated\":" + (truncated ? "true" : "false")
        + (namesOnly ? ",\"names\":[" : ",\"symbols\":[") + items + "]}";
  }

//+------------------------------------------------------------------+
//| True when the SERVER carries this symbol, in Market Watch or not.  |
//| Walking the tree is the definitive test: MarketInfo on an unknown   |
//| symbol merely returns 0 and sets 4106, which is indistinguishable  |
//| from a property that happens to be zero.                           |
//+------------------------------------------------------------------+
bool SymbolKnownToServer(const string symbol)
  {
   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++)
      if(SymbolName(i, false) == symbol)
         return true;
   return false;
  }

//+------------------------------------------------------------------+
//| One symbol's specification.                                        |
//|                                                                    |
//| PORT NOTE: MarketInfo() throughout, which is MT4's only complete    |
//| answer for a symbol. MarketInfo returns 0 for a symbol outside      |
//| Market Watch, so a symbol that is not there is added, read, and     |
//| removed again -- an operator's Market Watch is theirs, and a probe  |
//| that silently grew it by two hundred rows would be its own bug.     |
//+------------------------------------------------------------------+
string SymbolSpecJson(const string symbol, bool awaitPrice = false)
  {
   if(!SymbolKnownToServer(symbol))
      return "{\"symbol\":" + JsonQuote(symbol) + ",\"exists\":false}";

   bool wasSelected = (SymbolInfoInteger(symbol, SYMBOL_SELECT) > 0);
   if(!wasSelected)
      SymbolSelect(symbol, true);

   if(awaitPrice)
      AwaitPrice(symbol);

   int    digits = (int)MarketInfo(symbol, MODE_DIGITS);
   double point  = MarketInfo(symbol, MODE_POINT);
   double tick   = MarketInfo(symbol, MODE_TICKSIZE);
   double vstep  = MarketInfo(symbol, MODE_LOTSTEP);
   int    vd     = VolumeDigits(vstep > 0.0 ? vstep : 0.01);

   string js = "{\"symbol\":" + JsonQuote(symbol)
             + ",\"exists\":true"
             + ",\"in_market_watch\":" + (wasSelected ? "true" : "false")
             + ",\"digits\":" + IntegerToString(digits)
             + ",\"point\":" + DoubleToString(point, 8)
             // tick_size reads 0 on some MT4 servers, where the hub falls
             // back to point. Sent as the server states it rather than
             // silently substituted, so the fallback stays visible.
             + ",\"tick_size\":" + DoubleToString(tick, 8)
             + ",\"tick_value\":" + DoubleToString(MarketInfo(symbol, MODE_TICKVALUE), 8)
             + ",\"contract_size\":" + DoubleToString(MarketInfo(symbol, MODE_LOTSIZE), 2)
             + ",\"volume_step\":" + DoubleToString(vstep, vd)
             + ",\"volume_min\":" + DoubleToString(MarketInfo(symbol, MODE_MINLOT), vd)
             + ",\"volume_max\":" + DoubleToString(MarketInfo(symbol, MODE_MAXLOT), vd)
             + ",\"bid\":" + DoubleToString(MarketInfo(symbol, MODE_BID), digits)
             + ",\"ask\":" + DoubleToString(MarketInfo(symbol, MODE_ASK), digits)
             // MT4 has no SYMBOL_TRADE_MODE enum: MODE_TRADEALLOWED is a
             // flag. Reported in the same words the MT5 bridge uses, so a
             // reader is not comparing two vocabularies.
             + ",\"trade_mode\":"
             + JsonQuote(MarketInfo(symbol, MODE_TRADEALLOWED) > 0.0 ? "FULL" : "DISABLED")
             + ",\"margin_per_lot\":"
             + DoubleToString(MarketInfo(symbol, MODE_MARGINREQUIRED), 2)
             + ",\"expiry\":"
             + JsonQuote(ExpiryString((datetime)(long)MarketInfo(symbol, MODE_EXPIRATION)))
             + "}";

   if(!wasSelected)
      SymbolSelect(symbol, false);
   return js;
  }

//+------------------------------------------------------------------+
//| MARGIN -- what the broker would hold for this order.               |
//|                                                                    |
//| PORT NOTE, and it is a real difference: MT4 has no OrderCalcMargin. |
//| MODE_MARGINREQUIRED is margin per lot for a STANDALONE position and |
//| takes no direction, so unlike the MT5 bridge this figure cannot     |
//| differ between BUY and SELL. On a hedging account -- which every    |
//| MT4 account is -- that is the honest answer anyway: a second leg    |
//| the other way holds its own margin rather than offsetting.          |
//|                                                                    |
//| `direction` is accepted and ignored rather than rejected, so one    |
//| hub can ask both bridges the same question.                        |
//+------------------------------------------------------------------+
string HandleMargin(string json)
  {
   string symbol = JsonGetString(json, "symbol");
   double lots   = JsonGetDouble(json, "lots");

   if(!SymbolKnownToServer(symbol))
      return JsonError(symbol + " is not carried by this server");
   if(lots <= 0.0)
      return JsonError("lots must be above 0 to price margin");

   bool wasSelected = (SymbolInfoInteger(symbol, SYMBOL_SELECT) > 0);
   if(!wasSelected)
      SymbolSelect(symbol, true);

   double perLot = MarketInfo(symbol, MODE_MARGINREQUIRED);

   if(!wasSelected)
      SymbolSelect(symbol, false);

   if(perLot <= 0.0)
      return JsonError("this server reports no margin requirement for "
                       + symbol + ", so an order on it cannot be priced");

   return "{\"status\":\"ok\""
        + ",\"symbol\":" + JsonQuote(symbol)
        + ",\"margin\":" + DoubleToString(perLot * lots, 2)
        + ",\"margin_per_lot\":" + DoubleToString(perLot, 2)
        + ",\"margin_used\":" + DoubleToString(AccountMargin(), 2)
        + ",\"margin_free\":" + DoubleToString(AccountFreeMargin(), 2)
        + "}";
  }

//+------------------------------------------------------------------+
//| BARS -- completed bars, for the hub to compute this slave's ATR.   |
//|                                                                    |
//| Autonomous mode sets its stop from the SLAVE's volatility: the      |
//| master states how many ATRs away its stop sat, and that multiple    |
//| has to be measured against the instrument actually being traded.    |
//|                                                                    |
//| {"cmd":"BARS","symbol":"EURUSD","timeframe":"D1","count":20}        |
//|   -> {"status":"ok","count":20,"bars":[{"h":..,"l":..,"c":..},..]}  |
//|                                                                    |
//| Oldest first, because the hub walks them forward using the previous |
//| close as the gap reference. Reversed, every gap inverts and nothing |
//| fails.                                                             |
//+------------------------------------------------------------------+
string HandleBars(string json)
  {
   string symbol = JsonGetString(json, "symbol");
   string tfName = JsonGetString(json, "timeframe");
   int    count  = (int)JsonGetDouble(json, "count");

   if(count <= 0)
      count = 20;
   if(count > BarsReplyMax)
      count = BarsReplyMax;   // a reply has to fit the socket

   if(!SymbolKnownToServer(symbol))
      return JsonError(symbol + " is not carried by this server");

   int tf = TimeframeFromName(tfName);
   if(tf < 0)
      return JsonError("unknown timeframe '" + tfName + "'");

   bool wasSelected = (SymbolInfoInteger(symbol, SYMBOL_SELECT) > 0);
   if(!wasSelected)
      SymbolSelect(symbol, true);

   MqlRates rates[];
   // Oldest first.
   ArraySetAsSeries(rates, false);

   // start_pos 1, NOT 0. Bar 0 is still forming, and a partial range reads
   // as a quiet session -- which sizes the next position too large, in the
   // direction nobody notices. It also matches how the strategies set
   // their own stops: on the open of bar 1.
   int got = 0;
   for(int attempt = 0; attempt < 20; attempt++)      // ~2s
     {
      got = CopyRates(symbol, (ENUM_TIMEFRAMES)tf, 1, count, rates);
      if(got >= count)
         break;
      // A symbol just added, or a timeframe never opened on a chart, has
      // no history until the terminal fetches it -- and the first call is
      // what ASKS for it. Returning here would make every cold symbol a
      // permanent failure.
      Sleep(100);
     }

   if(!wasSelected)
      SymbolSelect(symbol, false);

   if(got <= 0)
      return JsonError("no " + tfName + " history for " + symbol
                       + " (CopyRates error " + IntegerToString(GetLastError()) + ")");

   int    d     = (int)MarketInfo(symbol, MODE_DIGITS);
   string items = "";
   for(int i = 0; i < got; i++)
     {
      if(i > 0)
         items += ",";
      items += "{\"h\":" + DoubleToString(rates[i].high,  d)
             + ",\"l\":" + DoubleToString(rates[i].low,   d)
             + ",\"c\":" + DoubleToString(rates[i].close, d) + "}";
     }

   // `count` is what was DELIVERED, not what was asked for. The hub
   // decides whether that is enough for the period it wants; a reply that
   // overstated it would have the hub compute an ATR from thin air.
   return "{\"status\":\"ok\",\"symbol\":" + JsonQuote(symbol)
        + ",\"timeframe\":" + JsonQuote(tfName)
        + ",\"count\":" + IntegerToString(got)
        + ",\"bars\":[" + items + "]}";
  }

//+------------------------------------------------------------------+
//| The hub names timeframes the way MQL does. -1 means unrecognised:  |
//| refused, never quietly defaulted, because a stop measured on the    |
//| wrong timeframe is wrong without looking it.                       |
//+------------------------------------------------------------------+
int TimeframeFromName(const string name)
  {
   string n = name;
   StringToUpper(n);
   if(n == "M1")  return PERIOD_M1;
   if(n == "M5")  return PERIOD_M5;
   if(n == "M15") return PERIOD_M15;
   if(n == "M30") return PERIOD_M30;
   if(n == "H1")  return PERIOD_H1;
   if(n == "H4")  return PERIOD_H4;
   if(n == "D1")  return PERIOD_D1;
   if(n == "W1")  return PERIOD_W1;
   if(n == "MN1") return PERIOD_MN1;
   return -1;
  }

//+------------------------------------------------------------------+
//| A symbol added this instant has no bid or ask until it ticks, and a |
//| spec reporting 0.00 is useless to a caller sizing a position.       |
//+------------------------------------------------------------------+
bool AwaitPrice(const string symbol)
  {
   for(int attempt = 0; attempt < 20; attempt++)   // ~1s
     {
      if(MarketInfo(symbol, MODE_BID) > 0.0 && MarketInfo(symbol, MODE_ASK) > 0.0)
         return true;
      Sleep(50);
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Decimals enough to print this volume step without rounding it to   |
//| nothing: a 0.001 step printed at 2dp reads as 0.00.                |
//+------------------------------------------------------------------+
int VolumeDigits(double step)
  {
   for(int d = 0; d <= 8; d++)
     {
      double scaled = step * MathPow(10, d);
      if(MathAbs(scaled - MathRound(scaled)) < 0.0000001)
         return d;
     }
   return 2;
  }

//+------------------------------------------------------------------+
//| An expiry as a date, or "" for a symbol that has none. Most MT4    |
//| CFDs report 0 here; the futures CFDs do not, and that date is the  |
//| whole reason a roll exists.                                        |
//+------------------------------------------------------------------+
string ExpiryString(datetime when)
  {
   if(when <= 0)
      return "";
   return TimeToString(when, TIME_DATE);
  }

//+------------------------------------------------------------------+
//| JSON output helpers                                                |
//+------------------------------------------------------------------+
string JsonError(string message)
  {
   StringReplace(message, "\\", "/");
   StringReplace(message, "\"", "'");
   return "{\"status\":\"error\",\"message\":\"" + message + "\"}";
  }

//+------------------------------------------------------------------+
//| A JSON string VALUE, quotes included, with the characters that      |
//| would break the hub's json.loads() escaped rather than stripped.    |
//| A server name or company occasionally carries a stray control       |
//| character, and one raw backslash makes the whole reply unparseable  |
//| -- which looks exactly like a dead bridge.                          |
//+------------------------------------------------------------------+
string JsonQuote(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   StringReplace(s, "\t", " ");
   return "\"" + s + "\"";
  }

//+------------------------------------------------------------------+
//| true only for a literal `true`. An absent key is false, which is   |
//| the right default for every flag the hub sends.                    |
//+------------------------------------------------------------------+
bool JsonGetBool(string json, string key)
  {
   int p = FindValueStart(json, key);
   if(p < 0)
      return false;
   return (StringSubstr(json, p, 4) == "true");
  }

//+------------------------------------------------------------------+
//| The string entries of a JSON array value, e.g. "symbols":["A","B"].|
//| Returns how many were read, so an absent key and an empty array    |
//| both come back 0 and callers can treat them alike. Non-string      |
//| entries stop the scan: no command needs one, and silently reading  |
//| past a number would be worse than returning the prefix.            |
//+------------------------------------------------------------------+
int JsonGetStringArray(string json, string key, string &out[], int limit = 64)
  {
   ArrayResize(out, 0);
   int p = FindValueStart(json, key);
   if(p < 0 || p >= StringLen(json) || StringGetCharacter(json, p) != '[')
      return 0;

   int len = StringLen(json);
   int n   = 0;
   p++;
   while(p < len && n < limit)
     {
      p = SkipWhitespace(json, p);
      if(p >= len)
         break;
      ushort c = StringGetCharacter(json, p);
      if(c == ']')
         break;
      if(c == ',')
        {
         p++;
         continue;
        }
      if(c != '"')
         break;
      p++;
      int end = StringFind(json, "\"", p);
      if(end < 0)
         break;
      ArrayResize(out, n + 1);
      out[n] = StringSubstr(json, p, end - p);
      n++;
      p = end + 1;
     }
   return n;
  }

//+------------------------------------------------------------------+
//| JSON input helpers                                                 |
//+------------------------------------------------------------------+
int SkipWhitespace(string json, int i)
  {
   int len = StringLen(json);
   while(i < len)
     {
      ushort c = StringGetCharacter(json, i);
      // Escape sequences, NOT raw characters. These were written as
      // literal control bytes, and line-ending normalisation had already
      // turned the CR into a second LF -- so the carriage-return case
      // stopped matching, silently. It does not bite while the only
      // caller is json.dumps(), which emits no bare CR; it would the
      // moment anything else spoke to this bridge.
      if(c == ' ' || c == '\t' || c == '\n' || c == '\r')
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
