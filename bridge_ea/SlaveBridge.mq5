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
//|   1.02 2026-11-11 CLOSE reports the fill price                  |
//|   1.03 2026-11-12 SYMBOLS and ACCOUNT commands; volume floors    |
//|                   to the step instead of rounding to nearest     |
//|   1.04 2026-11-12 SL/TP on the tick grid; a volume above the      |
//|                   symbol's maximum opens as a position group      |
//|   1.05 2026-11-12 MARGIN command, from OrderCalcMargin            |
//+------------------------------------------------------------------+
#property version   "1.05"
#define   BRIDGE_VERSION "1.05"

#include <Zmq/Zmq.mqh>

input int  HubCommandPort       = 5560;  // this slave's REP port -- must match the
                                         // "port" set for this slave in hub/config/slaves.json
input int  PollIntervalMs       = 100;   // how often to poll for a command
input int  Slippage             = 5;     // deviation (points) allowed on market orders
input long MagicNumber          = 0;     // stamped on every copier position
input int  SymbolsReplyMax      = 200;   // most symbols one SYMBOLS reply may carry,
                                         // for the "match" and Market Watch forms
input bool AllowNettingAccount  = false; // see the OnInit guard -- read it before setting true
input bool AllowVolumeSplit     = true;  // open a volume above the symbol's maximum as
                                         // several deals held together as one group

Context zmqContext("bridge_ea_slave");
Socket  repSocket(zmqContext, ZMQ_REP);

//+------------------------------------------------------------------+
//| POSITION GROUPS                                                   |
//|                                                                   |
//| A symbol's SYMBOL_VOLUME_MAX can be far below a sized position:   |
//| Darwinex caps MBT_X at 20 lots, and a 1%-of-balance bitcoin trade |
//| on that account is 200. The volume has to arrive as ten deals.    |
//|                                                                   |
//| That collides with the hub, which maps one master ticket to ONE   |
//| slave ticket -- the Trade Registry is keyed UNIQUE(slave_id,      |
//| master_ticket, slave_symbol) and get_open_mappings() hands the    |
//| engine a single slave_ticket. Ten rows would reconcile as one     |
//| tracked position and nine untracked ones.                         |
//|                                                                   |
//| So the legs are held together HERE and the hub is told about one  |
//| ticket: the first leg's, the group leader. CLOSE and MODIFY on    |
//| that ticket act on every leg, and POSITIONS reports the group as  |
//| one aggregated row. The hub's model does not change at all.       |
//|                                                                   |
//| Group membership lives in two places on purpose. The arrays below |
//| are authoritative while this EA runs; the leader's ticket and the |
//| leg count are also written into each follower's comment, so that  |
//| a terminal restart or a recompile can rediscover a group from the |
//| positions themselves instead of orphaning its legs. Brokers are   |
//| allowed to rewrite comments, which is why the count travels with  |
//| them: a group that finds fewer legs than it declares says so      |
//| rather than quietly closing part of itself.                       |
//+------------------------------------------------------------------+
#define MAX_LEGS  64
#define GROUP_TAG "cpg:"

ulong g_leader[];              // leader ticket, one entry per live group
ulong g_legs[][MAX_LEGS];      // its legs, including the leader at [0]
int   g_legCount[];

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
   if(cmd == "SYMBOLS")
      return HandleSymbols(json);
   if(cmd == "ACCOUNT")
      return HandleAccount();
   if(cmd == "MARGIN")
      return HandleMargin(json);

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
   // Above-maximum passes the normaliser when splitting is on, because the
   // split below is what makes it tradeable. Refusing it in both places would
   // make the two disagree about what this EA can do.
   double volume = NormalizeVolume(symbol, lots, volErr, AllowVolumeSplit);
   if(volErr != "")
      return JsonError(volErr);

   int    digits  = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double slLevel = (sl > 0.0) ? OnTickGrid(symbol, sl) : 0.0;
   double tpLevel = (tp > 0.0) ? OnTickGrid(symbol, tp) : 0.0;
   ENUM_ORDER_TYPE otype = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   // Checked once, before any clip is sent: a stop the broker will reject
   // should not cost ten round trips to discover.
   string stopsErr = CheckStopsDistance(symbol, otype, NormalizeDouble(price, digits),
                                        slLevel, tpLevel);
   if(stopsErr != "")
      return JsonError(stopsErr);

   double clips[];
   int    nclips = SplitVolume(symbol, volume, clips);
   if(nclips < 0)
      return JsonError(StringFormat(
         "%.2f lots needs more than %d clips of %s's maximum %.2f -- refusing rather "
         "than opening a position this EA cannot then close as one unit",
         volume, MAX_LEGS, symbol, SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX)));

   ulong  legs[];
   int    done     = 0;
   double filled   = 0.0;
   double costSum  = 0.0;
   string lastErr  = "";

   for(int i = 0; i < nclips; i++)
     {
      ulong  t  = 0;
      double fp = 0.0, fl = 0.0;
      string err = "";

      // The leader's ticket cannot be known before it exists, so the first
      // clip carries the plain comment and every later clip is tagged with
      // the leader and the leg count. The group is therefore rediscoverable
      // from the positions themselves, with no state in this EA to lose
      // across a restart or a recompile -- see ResolveGroupLegs.
      string tag = (done == 0)
                   ? "copier"
                   : GROUP_TAG + IntegerToString((long)legs[0]) + ":"
                     + IntegerToString(nclips);

      if(!OpenOneDeal(symbol, otype, clips[i], slLevel, tpLevel, digits, tag,
                      t, fp, fl, err))
        {
         lastErr = err;
         break;
        }

      ArrayResize(legs, done + 1);
      legs[done] = t;
      done++;
      filled  += fl;
      costSum += fp * fl;
     }

   // Nothing opened at all is the plain failure the caller already knows.
   if(done == 0)
      return JsonError(lastErr);

   // Something opened and something did not. Deliberately NOT rolled back:
   // the hub records the size that filled, exactly as it does for a partial
   // fill, and unwinding here could fail too and leave a worse mess. It is
   // reported in the reply so it cannot pass unnoticed.
   if(lastErr != "")
      Print("SlaveBridge WARNING: ", symbol, " split short -- ", done, " of ", nclips,
            " clips opened (", filled, " of ", volume, " lots). Refusal: ", lastErr);

   if(done > 1)
      GroupRemember(legs[0], legs, done);

   double vwap = (filled > 0.0) ? costSum / filled : 0.0;

   string js = "{\"status\":\"ok\",\"slave_ticket\":" + IntegerToString((long)legs[0])
             + ",\"price\":" + DoubleToString(vwap, digits)
             + ",\"lots\":" + DoubleToString(filled, 2)
             + ",\"legs\":" + IntegerToString(done);
   if(lastErr != "")
      js += ",\"warning\":" + JsonQuote(StringFormat(
               "only %d of %d clips filled (%.2f of %.2f lots): %s",
               done, nclips, filled, volume, lastErr));
   return js + "}";
  }

//| Group bookkeeping. Linear scans over a handful of groups: a slave  |
//| holds tens of positions, not thousands, and a map would be more    |
//| machinery than the problem has.                                    |
int GroupIndex(ulong leader)
  {
   for(int i = 0; i < ArraySize(g_leader); i++)
      if(g_leader[i] == leader)
         return i;
   return -1;
  }

void GroupRemember(ulong leader, ulong &legs[], int n)
  {
   if(n > MAX_LEGS)
      n = MAX_LEGS;
   int idx = GroupIndex(leader);
   if(idx < 0)
     {
      idx = ArraySize(g_leader);
      ArrayResize(g_leader, idx + 1);
      ArrayResize(g_legs, idx + 1);
      ArrayResize(g_legCount, idx + 1);
     }
   g_leader[idx]   = leader;
   g_legCount[idx] = n;
   for(int i = 0; i < n; i++)
      g_legs[idx][i] = legs[i];
  }

void GroupForget(ulong leader)
  {
   int idx = GroupIndex(leader);
   if(idx < 0)
      return;
   int last = ArraySize(g_leader) - 1;
   if(idx != last)
     {
      g_leader[idx]   = g_leader[last];
      g_legCount[idx] = g_legCount[last];
      for(int i = 0; i < g_legCount[last]; i++)
         g_legs[idx][i] = g_legs[last][i];
     }
   ArrayResize(g_leader, last);
   ArrayResize(g_legs, last);
   ArrayResize(g_legCount, last);
  }

//| The leader a position belongs to: the ticket encoded in its comment |
//| for a follower, or its own ticket for anything else. Takes the      |
//| comment as read, since the caller has the position selected.        |
ulong GroupLeaderOf(ulong ticket, const string comment)
  {
   if(StringFind(comment, GROUP_TAG) != 0)
      return ticket;
   int colon = StringFind(comment, ":", StringLen(GROUP_TAG));
   if(colon < 0)
      return ticket;
   string digits = StringSubstr(comment, StringLen(GROUP_TAG),
                                colon - StringLen(GROUP_TAG));
   ulong  leader = (ulong)StringToInteger(digits);
   return (leader > 0) ? leader : ticket;
  }

//| The legs of a group that still EXIST, leader included. Returns the  |
//| count, which is 0 when nothing is open -- so a caller can tell an   |
//| already-closed group from a live one without a second question.     |
//|                                                                     |
//| The leader not existing does not end the search: a leader closed by |
//| hand must not strand the nine legs behind it, which is exactly what |
//| returning "already closed" here would do.                           |
int ResolveGroupLegs(ulong leader, ulong &out[])
  {
   ArrayResize(out, 0);
   int n = 0;

   if(PositionSelectByTicket(leader))
     {
      ArrayResize(out, 1);
      out[0] = leader;
      n = 1;
     }

   int idx = GroupIndex(leader);
   if(idx >= 0)
     {
      for(int i = 0; i < g_legCount[idx]; i++)
        {
         ulong t = g_legs[idx][i];
         if(t == leader || !PositionSelectByTicket(t))
            continue;
         ArrayResize(out, n + 1);
         out[n] = t;
         n++;
        }
      return n;
     }

   // Nothing in memory: rediscover from the comments. This is the path after
   // a terminal restart, a recompile, or a re-attach.
   string want     = GROUP_TAG + IntegerToString((long)leader) + ":";
   int    declared = 0;
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || t == leader)
         continue;
      string comment = PositionGetString(POSITION_COMMENT);
      if(StringFind(comment, want) != 0)
         continue;
      declared = (int)StringToInteger(StringSubstr(comment, StringLen(want)));
      ArrayResize(out, n + 1);
      out[n] = t;
      n++;
     }

   if(declared > 0 && declared != n)
      Print("SlaveBridge WARNING: group ", leader, " declares ", declared,
            " legs but ", n, " were found. A broker may have rewritten the ",
            "comments; the legs not found are NOT being acted on -- look at ",
            "this account by hand.");
   return n;
  }

//| One deal. Extracted from HandleOpen so a split can send several    |
//| without the body being written twice, and so a clip that fails can |
//| say why without unwinding the clips that worked.                    |
bool OpenOneDeal(const string symbol, ENUM_ORDER_TYPE otype, double volume,
                 double sl, double tp, int digits, const string comment,
                 ulong &posTicket, double &fillPrice, double &filledLots,
                 string &err)
  {
   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);

   // Re-read the price for every clip. Ten deals are ten moments, and reusing
   // the first clip's price for the tenth would aim the deviation window at a
   // price the market has already left.
   bool   isBuy = (otype == ORDER_TYPE_BUY);
   double price = isBuy ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                        : SymbolInfoDouble(symbol, SYMBOL_BID);

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = symbol;
   req.volume       = volume;
   req.type         = otype;
   req.price        = NormalizeDouble(price, digits);
   // A master SL/TP of 0 means "unset", exactly as in MQL4 -- and 0 is also
   // how MQL5 says "no level". Non-zero levels arrive already on the tick
   // grid; see OnTickGrid, and MBT_X, whose tick is 5 whole dollars.
   req.sl           = sl;
   req.tp           = tp;
   req.deviation    = (ulong)Slippage;
   req.magic        = (ulong)MagicNumber;
   req.comment      = comment;
   req.type_filling = PickFilling(symbol);

   if(!OrderSend(req, res))
     {
      err = StringFormat("OrderSend failed to submit: retcode=%d %s (last_error=%d)",
                         (int)res.retcode, res.comment, GetLastError());
      return false;
     }

   if(res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL)
     {
      err = StringFormat("OrderSend rejected: retcode=%d %s",
                         (int)res.retcode, res.comment);
      return false;
     }

   if(res.retcode == TRADE_RETCODE_DONE_PARTIAL)
      Print("SlaveBridge WARNING: ", symbol, " filled only ", res.volume, " of ",
            volume, " -- the hub will track the position at the filled size.");

   //--- PORT NOTE: MqlTradeResult has no position ticket, and the hub needs
   // one: every later MODIFY/CLOSE arrives keyed by whatever we return here.
   // On a hedging account the position ticket equals the opening order's
   // ticket, but the position can take a moment to appear, and on a netting
   // account it is the pre-existing position's id instead -- so resolve it
   // properly rather than assuming res.order.
   posTicket = ResolvePositionTicket(res.order, res.deal);
   if(posTicket == 0)
     {
      err = StringFormat(
         "order %s filled but its position ticket could not be resolved -- the "
         "position is OPEN AND UNTRACKED, close it by hand",
         IntegerToString((long)res.order));
      return false;
     }

   fillPrice  = (res.price > 0.0) ? res.price : req.price;
   filledLots = (res.volume > 0.0) ? res.volume : volume;
   return true;
  }

//| Cuts a volume into clips the broker will accept one at a time.     |
//| Returns the clip count, or -1 when it would need more legs than    |
//| MAX_LEGS -- refused rather than truncated, because a position this |
//| EA cannot close as one unit is worse than an order that never      |
//| opened. A volume at or below the maximum is one clip, so the        |
//| single-order path is just the n=1 case and is not special-cased.    |
int SplitVolume(const string symbol, double total, double &clips[])
  {
   double vmax = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      step = 0.01;
   int vd = VolumeDigits(step);

   ArrayResize(clips, 0);
   if(vmax <= 0.0 || total <= vmax + 1e-8)
     {
      ArrayResize(clips, 1);
      clips[0] = NormalizeDouble(total, vd);
      return 1;
     }

   double left = total;
   int    n    = 0;
   while(left > 1e-8)
     {
      if(n >= MAX_LEGS)
         return -1;
      double clip = (left > vmax) ? vmax : left;
      ArrayResize(clips, n + 1);
      clips[n] = NormalizeDouble(clip, vd);
      n++;
      left = NormalizeDouble(left - clip, vd);
     }
   return n;
  }

//+------------------------------------------------------------------+
string HandleModify(string json)
  {
   ulong  ticket = JsonGetTicket(json, "ticket");
   double sl     = JsonGetDouble(json, "sl");
   double tp     = JsonGetDouble(json, "tp");

   // A split position is several tickets wearing one ticket's name, so the
   // stop has to move on all of them or the group ends up with legs stopping
   // out at different prices -- which is the one thing a copied stop must
   // never do.
   ulong legs[];
   int   n = ResolveGroupLegs(ticket, legs);
   if(n > 1)
      return ModifyEveryLeg(legs, n, sl, tp);
   if(n == 1)
      // Possibly a group worn down to one leg, and possibly not the leader
      // the hub asked for. Move the stop on whatever is actually open, or a
      // live leg would be reported as already closed and left unprotected.
      ticket = legs[0];

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

   string err = ModifyOneLeg(ticket, sl, tp);
   if(err != "")
      return JsonError(err);
   return "{\"status\":\"ok\"}";
  }

//| One leg's SL/TP. Shared by the single-position path and the group   |
//| path so the two cannot drift apart. Returns "" on success.          |
string ModifyOneLeg(ulong ticket, double sl, double tp)
  {
   if(!PositionSelectByTicket(ticket))
      return StringFormat("ticket %s not found", IntegerToString((long)ticket));

   string symbol = PositionGetString(POSITION_SYMBOL);

   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action   = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.symbol   = symbol;
   req.sl       = (sl > 0.0) ? OnTickGrid(symbol, sl) : 0.0;
   req.tp       = (tp > 0.0) ? OnTickGrid(symbol, tp) : 0.0;

   ENUM_ORDER_TYPE asOrder = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
                             ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   string stopsErr = CheckStopsDistance(symbol, asOrder,
                                        PositionGetDouble(POSITION_PRICE_CURRENT),
                                        req.sl, req.tp);
   if(stopsErr != "")
      return stopsErr;

   if(!OrderSend(req, res))
      return StringFormat("SLTP failed to submit: retcode=%d %s (last_error=%d)",
                          (int)res.retcode, res.comment, GetLastError());

   // 10025 means the levels already match what was asked for. MQL4's
   // OrderModify returned false with error 1 for the same case; either way it
   // is the outcome MODIFY wanted, so it is not an error.
   if(res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_NO_CHANGES)
      return StringFormat("SLTP rejected: retcode=%d %s", (int)res.retcode, res.comment);

   return "";
  }

//| Every leg of a split position, or an error naming the ones that     |
//| refused. Partial success is reported as an ERROR on purpose: a      |
//| group whose legs hold different stops is a position whose real risk |
//| nobody knows, and the hub marking the MODIFY as done would hide it. |
string ModifyEveryLeg(ulong &legs[], int n, double sl, double tp)
  {
   int    moved  = 0;
   string failed = "";
   for(int i = 0; i < n; i++)
     {
      string err = ModifyOneLeg(legs[i], sl, tp);
      if(err == "")
        {
         moved++;
         continue;
        }
      if(failed != "")
         failed += "; ";
      failed += IntegerToString((long)legs[i]) + ": " + err;
     }

   if(failed != "")
      return JsonError(StringFormat(
         "group %s: %d of %d legs took the new levels, %d refused -- the legs now "
         "hold DIFFERENT stops. %s", IntegerToString((long)legs[0]), moved, n,
         n - moved, failed));

   return "{\"status\":\"ok\",\"legs\":" + IntegerToString(n) + "}";
  }

//+------------------------------------------------------------------+
string HandleClose(string json)
  {
   ulong ticket = JsonGetTicket(json, "ticket");

   // Resolved BEFORE the already-closed check, because a leader closed by
   // hand must not be reported as a finished CLOSE while nine legs behind it
   // are still open -- the registry row would go to CLOSED and nothing would
   // be tracking them.
   ulong legs[];
   int   n = ResolveGroupLegs(ticket, legs);
   if(n > 1)
      return CloseEveryLeg(ticket, legs, n);

   if(n == 0)
     {
      // Already flat -- closed by hand, or its SL/TP fired before the
      // master's CLOSE arrived. That is the outcome CLOSE was asking for, so
      // report success: an error here leaves the registry row stuck at OPEN.
      // See the PORT NOTE in HandleModify for why this needs history.
      GroupForget(ticket);
      if(PositionExistedAndClosed(ticket))
         return "{\"status\":\"ok\",\"already_closed\":true,\"slave_ticket\":"
              + IntegerToString((long)ticket) + "}";
      return JsonError("ticket " + IntegerToString((long)ticket) + " not found");
     }

   // One leg, which may be the sole survivor of a group rather than the
   // leader the hub asked for; close what exists either way.
   ticket = legs[0];
   if(!PositionSelectByTicket(ticket))
      return JsonError("ticket " + IntegerToString((long)ticket) + " vanished mid-close");

   GroupForget(legs[0]);

   double price  = 0.0;
   int    digits = 0;
   string err    = "";
   if(!CloseOneLeg(ticket, price, digits, err))
      return JsonError(err);

   return "{\"status\":\"ok\",\"price\":" + DoubleToString(price, digits) + "}";
  }

//| One leg closed. Shared by the single-position path and the group    |
//| path. `digits` comes back because the symbol is gone by the time    |
//| the caller formats the price.                                        |
bool CloseOneLeg(ulong ticket, double &price, int &digits, string &err)
  {
   if(!PositionSelectByTicket(ticket))
     {
      err = StringFormat("ticket %s not found", IntegerToString((long)ticket));
      return false;
     }

   string symbol = PositionGetString(POSITION_SYMBOL);
   double volume = PositionGetDouble(POSITION_VOLUME);
   bool   isLong = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

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
     {
      err = StringFormat("close failed to submit: retcode=%d %s (last_error=%d)",
                         (int)res.retcode, res.comment, GetLastError());
      return false;
     }

   if(res.retcode == TRADE_RETCODE_DONE_PARTIAL)
     {
      // Deliberately a failure. The hub marks the registry row CLOSED on
      // success, which would abandon the remaining volume with nothing
      // tracking it. Reporting failure keeps the row OPEN and puts the
      // leftover in front of a human.
      double left = PositionSelectByTicket(ticket) ? PositionGetDouble(POSITION_VOLUME) : 0.0;
      err = StringFormat(
         "close only partially filled: %.2f of %.2f lots remain on ticket %s",
         left, volume, IntegerToString((long)ticket));
      return false;
     }

   if(res.retcode != TRADE_RETCODE_DONE)
     {
      err = StringFormat("close rejected: retcode=%d %s", (int)res.retcode, res.comment);
      return false;
     }

   // The deal price, not the price requested: the difference is the exit
   // slippage, and it is reported once or not at all. OPEN already does
   // this via res.price.
   price = (res.price > 0.0) ? res.price : req.price;
   return true;
  }

//| Every leg of a split position. Any leg left open is an ERROR, even  |
//| if nine of ten closed: the hub marks the registry row CLOSED on     |
//| success, and a row marked closed over a live position is how volume |
//| ends up on an account with nothing tracking it. Failing keeps the   |
//| row OPEN and names the tickets.                                      |
string CloseEveryLeg(ulong leader, ulong &legs[], int n)
  {
   GroupForget(leader);

   int    closed  = 0;
   int    digits  = 0;
   double costSum = 0.0;
   double lotsSum = 0.0;
   string stuck   = "";

   for(int i = 0; i < n; i++)
     {
      // Read the size before closing it; afterwards the position is gone.
      double lots = PositionSelectByTicket(legs[i])
                    ? PositionGetDouble(POSITION_VOLUME) : 0.0;
      double price = 0.0;
      string err   = "";
      if(CloseOneLeg(legs[i], price, digits, err))
        {
         closed++;
         costSum += price * lots;
         lotsSum += lots;
         continue;
        }
      if(stuck != "")
         stuck += "; ";
      stuck += IntegerToString((long)legs[i]) + ": " + err;
     }

   if(stuck != "")
      return JsonError(StringFormat(
         "group %s: closed %d of %d legs, %d STILL OPEN -- %s",
         IntegerToString((long)leader), closed, n, n - closed, stuck));

   double vwap = (lotsSum > 0.0) ? costSum / lotsSum : 0.0;
   return "{\"status\":\"ok\",\"price\":" + DoubleToString(vwap, digits)
        + ",\"lots\":" + DoubleToString(lotsSum, 2)
        + ",\"legs\":" + IntegerToString(closed) + "}";
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
   // Accumulated per GROUP, not per position: a split position is one thing
   // the hub is tracking under one ticket, and reporting its ten legs
   // separately would make reconciliation see nine untracked positions and
   // warn about volume that is already accounted for.
   ulong  keys[];      // the group leader's ticket, which is what the hub holds
   string syms[];
   double qty[];       // signed, summed
   double absLots[];   // for the volume-weighted average entry
   double cost[];
   double pnl[];
   double slv[];
   double tpv[];
   int    legs[];
   int    digs[];
   int    g = 0;

   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong ticket = PositionGetTicket(i);   // also selects it
      if(ticket == 0)
         continue;

      string symbol  = PositionGetString(POSITION_SYMBOL);
      string comment = PositionGetString(POSITION_COMMENT);
      double lots    = PositionGetDouble(POSITION_VOLUME);
      bool   isLong  = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      double open    = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl      = PositionGetDouble(POSITION_SL);
      double tp      = PositionGetDouble(POSITION_TP);

      //--- PORT NOTE: MQL4 had OrderCommission() on the order itself. In MQL5
      // commission lives on the deals, not the position, so including it
      // would mean a HistorySelectByPosition() per position on a read the
      // dashboard polls every 5s. Profit + swap it is; the figure is a few
      // cents off the realisable amount rather than wrong.
      double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);

      ulong leader = GroupLeaderOf(ticket, comment);

      int k = -1;
      for(int j = 0; j < g; j++)
         if(keys[j] == leader)
           {
            k = j;
            break;
           }
      if(k < 0)
        {
         k = g;
         g++;
         ArrayResize(keys, g);    ArrayResize(syms, g);    ArrayResize(qty, g);
         ArrayResize(absLots, g); ArrayResize(cost, g);    ArrayResize(pnl, g);
         ArrayResize(slv, g);     ArrayResize(tpv, g);     ArrayResize(legs, g);
         ArrayResize(digs, g);
         keys[k] = leader;
         syms[k] = symbol;
         qty[k] = 0.0; absLots[k] = 0.0; cost[k] = 0.0; pnl[k] = 0.0;
         slv[k] = 0.0; tpv[k] = 0.0; legs[k] = 0;
         digs[k] = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
        }

      // Signed quantity, matching the hub's Position.quantity convention.
      qty[k]     += isLong ? lots : -lots;
      absLots[k] += lots;
      cost[k]    += open * lots;
      pnl[k]     += profit;
      legs[k]    += 1;
      // The leader's own levels win. A follower fills them in only while they
      // are unset, so a group mid-MODIFY reports a level rather than nothing.
      if(ticket == leader || (slv[k] == 0.0 && sl > 0.0))
         slv[k] = sl;
      if(ticket == leader || (tpv[k] == 0.0 && tp > 0.0))
         tpv[k] = tp;
     }

   string items = "";
   for(int k = 0; k < g; k++)
     {
      if(k > 0)
         items += ",";
      double avg = (absLots[k] > 0.0) ? cost[k] / absLots[k] : 0.0;
      items += "{\"ticket\":" + IntegerToString((long)keys[k])
             + ",\"symbol\":\"" + syms[k] + "\""
             + ",\"quantity\":" + DoubleToString(qty[k], 2)
             + ",\"average_price\":" + DoubleToString(avg, digs[k])
             // Reported so the hub can confirm a MODIFY landed; 0 means "not
             // set", which the hub reads as None rather than a stop at zero.
             + ",\"sl\":" + DoubleToString(slv[k], digs[k])
             + ",\"tp\":" + DoubleToString(tpv[k], digs[k])
             + ",\"unrealized_pnl\":" + DoubleToString(pnl[k], 2)
             // Extra, and ignored by an older hub: how many real positions
             // this one row stands for.
             + ",\"legs\":" + IntegerToString(legs[k])
             + "}";
     }

   return "{\"status\":\"ok\",\"positions\":[" + items + "]}";
  }

//+------------------------------------------------------------------+
//| SYMBOLS -- what this broker's instruments actually ARE. Read-only:|
//| it places nothing and leaves the Market Watch as it found it.     |
//|                                                                   |
//| New in 1.03. Contract size, volume min/max/step, tick size and    |
//| value, expiry, and whether a symbol is a futures CFD at all live  |
//| only in the terminal, and until now the only way to ask was to    |
//| send an OPEN and read the refusal. That does work -- OPEN checks  |
//| the symbol, then the price, then the volume, and builds no order  |
//| until all three pass -- but it yields one number per round trip   |
//| and needs a volume small enough to be refused, which is a poor    |
//| way to learn a contract size.                                     |
//|                                                                   |
//| Three forms:                                                      |
//|   {"cmd":"SYMBOLS","symbols":["US500","XTIUSD"]}                  |
//|        named symbols; an absent one comes back exists:false        |
//|        rather than as an error, so one dead name cannot hide the   |
//|        answer for the rest                                        |
//|   {"cmd":"SYMBOLS","match":"futures"}                             |
//|        case-insensitive substring over both the symbol name and   |
//|        its SYMBOL_PATH, across the server's whole tree            |
//|   {"cmd":"SYMBOLS"}                                               |
//|        the Market Watch as it stands                              |
//|                                                                   |
//| Two optional fields on any form. "limit" caps the reply below      |
//| SymbolsReplyMax, and "names_only":true returns bare names with no  |
//| specification read at all. Use names_only to explore: reading a    |
//| spec means briefly selecting the symbol, and two hundred of those  |
//| is two hundred Market Watch mutations and subscriptions, enough to |
//| stall OnTimer past the hub's request timeout. Explore with         |
//| names_only, then ask for the handful of specs that matter.         |
//|                                                                   |
//| The match form walks SymbolsTotal(false): every symbol the SERVER |
//| offers this account, not the subset displayed in Market Watch.    |
//| That distinction is the point of the command. A symbol missing    |
//| from that tree cannot be traded by this account at all, whereas a |
//| symbol missing from Market Watch is merely not being shown -- and |
//| the two are indistinguishable from the hub without this.          |
//+------------------------------------------------------------------+
string HandleSymbols(string json)
  {
   string names[];
   int    n     = JsonGetStringArray(json, "symbols", names);
   string match = JsonGetString(json, "match");
   bool   truncated = false;
   bool   namesOnly = JsonGetBool(json, "names_only");

   int limit = (int)JsonGetDouble(json, "limit");
   if(limit <= 0 || limit > SymbolsReplyMax)
      limit = SymbolsReplyMax;   // the input stays the hard ceiling

   if(n == 0)
     {
      // No explicit list, so either filter the server's whole tree or report
      // the Market Watch. SymbolsTotal/SymbolName take `selected`, hence the
      // named flag: passing the wrong one here would quietly answer the
      // other question.
      bool   watchOnly = (match == "");
      string needle    = match;
      StringToUpper(needle);

      int total = SymbolsTotal(watchOnly);
      for(int i = 0; i < total; i++)
        {
         string s = SymbolName(i, watchOnly);
         if(s == "")
            continue;
         // "*" is the whole tree: there is no other way to ask, since an
         // empty match is already taken to mean the Market Watch.
         if(!watchOnly && needle != "*")
           {
            // Name and path both, because a futures group is recognisable
            // from its path ("Futures\Energies\...") long before its symbol
            // names are. If a path reads empty for an unselected symbol the
            // name half still matches, so this degrades rather than breaks.
            string hay = s + "|" + SymbolInfoString(s, SYMBOL_PATH);
            StringToUpper(hay);
            if(StringFind(hay, needle) < 0)
               continue;
           }
         if(n >= limit)
           {
            truncated = true;    // reported, so a short list is never mistaken
            break;               // for a complete one
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
      items += namesOnly ? JsonQuote(names[i]) : SymbolSpecJson(names[i]);
     }

   return "{\"status\":\"ok\",\"count\":" + IntegerToString(n)
        + ",\"truncated\":" + (truncated ? "true" : "false")
        + (namesOnly ? ",\"names\":[" : ",\"symbols\":[") + items + "]}";
  }

//| True when the SERVER carries this symbol for this account, whether |
//| or not it is in Market Watch. Walking the tree is the definitive   |
//| test: SymbolInfo* on an unknown symbol merely sets 4301, which is  |
//| indistinguishable from a property that happens to be empty.        |
bool SymbolKnownToServer(const string symbol)
  {
   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++)
      if(SymbolName(i, false) == symbol)
         return true;
   return false;
  }

//| One symbol's specification.                                        |
//|                                                                    |
//| Properties are only reliably readable for a symbol in the Market   |
//| Watch, so a symbol that is not there is added, read, and then       |
//| removed again -- an operator's Market Watch is theirs, and a probe |
//| that silently grew it by two hundred rows would be its own bug.     |
string SymbolSpecJson(const string symbol)
  {
   if(!SymbolKnownToServer(symbol))
      return "{\"symbol\":" + JsonQuote(symbol) + ",\"exists\":false}";

   bool wasSelected = (bool)SymbolInfoInteger(symbol, SYMBOL_SELECT);
   if(!wasSelected)
      SymbolSelect(symbol, true);

   int    digits  = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   long   expiry  = (long)SymbolInfoInteger(symbol, SYMBOL_EXPIRATION_TIME);
   double vstep   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   int    vd      = VolumeDigits(vstep > 0.0 ? vstep : 0.01);

   // Stripped of their enum prefixes because these two are what a human
   // reads first: CALC_MODE says whether this is a futures CFD, FOREX or a
   // plain CFD, and TRADE_MODE says whether it can be opened at all (a
   // CLOSEONLY symbol resolves, quotes, and refuses every entry).
   string calcName = EnumToString((ENUM_SYMBOL_CALC_MODE)
                                  SymbolInfoInteger(symbol, SYMBOL_TRADE_CALC_MODE));
   StringReplace(calcName, "SYMBOL_CALC_MODE_", "");
   string tradeName = EnumToString((ENUM_SYMBOL_TRADE_MODE)
                                   SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE));
   StringReplace(tradeName, "SYMBOL_TRADE_MODE_", "");
   string fillName = EnumToString(PickFilling(symbol));
   StringReplace(fillName, "ORDER_FILLING_", "");

   string js = "{\"symbol\":" + JsonQuote(symbol)
             + ",\"exists\":true"
             + ",\"in_market_watch\":" + (wasSelected ? "true" : "false")
             + ",\"path\":" + JsonQuote(SymbolInfoString(symbol, SYMBOL_PATH))
             + ",\"description\":" + JsonQuote(SymbolInfoString(symbol, SYMBOL_DESCRIPTION))
             + ",\"calc_mode\":" + JsonQuote(calcName)
             + ",\"trade_mode\":" + JsonQuote(tradeName)
             + ",\"contract_size\":"
             + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE), 2)
             + ",\"volume_min\":"
             + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN), vd)
             + ",\"volume_max\":"
             + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX), vd)
             + ",\"volume_step\":" + DoubleToString(vstep, vd)
             + ",\"digits\":" + IntegerToString(digits)
             + ",\"point\":" + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_POINT), 8)
             + ",\"tick_size\":"
             + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE), 8)
             + ",\"tick_value\":"
             + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE), 5)
             + ",\"stops_level\":"
             + IntegerToString(SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL))
             + ",\"filling\":" + JsonQuote(fillName)
             + ",\"filling_mask\":"
             + IntegerToString(SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE))
             + ",\"currency_profit\":"
             + JsonQuote(SymbolInfoString(symbol, SYMBOL_CURRENCY_PROFIT))
             + ",\"currency_margin\":"
             + JsonQuote(SymbolInfoString(symbol, SYMBOL_CURRENCY_MARGIN))
             // A dated contract's expiry, 0 when the symbol is perpetual.
             // This is how the hub can tell a rolling CFD from a CFD that
             // holds one specific futures month.
             + ",\"expiry_ts\":" + IntegerToString(expiry)
             + ",\"expiry\":" + JsonQuote(expiry > 0
                                         ? TimeToString((datetime)expiry, TIME_DATE) : "")
             + ",\"bid\":" + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_BID), digits)
             + ",\"ask\":" + DoubleToString(SymbolInfoDouble(symbol, SYMBOL_ASK), digits)
             + "}";

   // Put the Market Watch back. A symbol that cannot be removed because a
   // chart or an open position holds it is not worth an error -- the reply
   // is already correct, and SYMBOL_SELECT above reported the true state.
   if(!wasSelected)
      SymbolSelect(symbol, false);

   return js;
  }

//+------------------------------------------------------------------+
//| ACCOUNT -- the figures a sizing model needs, plus the two fields  |
//| that turn a wrong-terminal mistake from a discovery into a check. |
//|                                                                   |
//| New in 1.03, and the reason is sizing: mirror mode scales the      |
//| master's lots by a constant (copy_engine._scaled_lots) because     |
//| MT4Adapter.get_account_summary raises NotImplementedError -- the   |
//| bridge had no way to say what the account is worth. Anything       |
//| phrased as a percentage of balance was therefore uncomputable on   |
//| an MT bridge, whoever was asking.                                  |
//|                                                                   |
//| trade_mode is DEMO, CONTEST or REAL, straight from the server.     |
//| Reported so that a caller can REFUSE a real account by rule        |
//| instead of by care, which matters here specifically: the Darwinex  |
//| demo and the Darwinex live terminal differ by a number in a title  |
//| bar, and the live one is not to be touched.                        |
//|                                                                   |
//| ACCOUNT_NAME is deliberately not reported. The holder's name is    |
//| personal data, no sizing model needs it, and this reply ends up in |
//| logs.                                                              |
//+------------------------------------------------------------------+
string HandleAccount()
  {
   string tradeMode = EnumToString((ENUM_ACCOUNT_TRADE_MODE)
                                   AccountInfoInteger(ACCOUNT_TRADE_MODE));
   StringReplace(tradeMode, "ACCOUNT_TRADE_MODE_", "");
   string marginMode = EnumToString((ENUM_ACCOUNT_MARGIN_MODE)
                                    AccountInfoInteger(ACCOUNT_MARGIN_MODE));
   StringReplace(marginMode, "ACCOUNT_MARGIN_MODE_", "");

   return "{\"status\":\"ok\""
        + ",\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))
        + ",\"server\":" + JsonQuote(AccountInfoString(ACCOUNT_SERVER))
        + ",\"company\":" + JsonQuote(AccountInfoString(ACCOUNT_COMPANY))
        + ",\"trade_mode\":" + JsonQuote(tradeMode)
        + ",\"margin_mode\":" + JsonQuote(marginMode)
        + ",\"currency\":" + JsonQuote(AccountInfoString(ACCOUNT_CURRENCY))
        + ",\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2)
        + ",\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2)
        + ",\"margin\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2)
        + ",\"margin_free\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2)
        + ",\"margin_level\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2)
        + ",\"leverage\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE))
        // Two separate switches, and a trade fails with 10027 if either is
        // off while everything else looks healthy.
        + ",\"trade_allowed\":"
        + ((AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)
            && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) ? "true" : "false")
        + ",\"algo_allowed\":"
        + (MQLInfoInteger(MQL_TRADE_ALLOWED) ? "true" : "false")
        + ",\"bridge_version\":" + JsonQuote(BRIDGE_VERSION)
        + "}";
  }

//+------------------------------------------------------------------+
//| MARGIN -- what the broker would actually hold for an order this   |
//| size, from the broker's own calculation. Places nothing.          |
//|                                                                   |
//| New in 1.05, and it closes a real gap. A risk model bounds the    |
//| loss at the stop and says NOTHING about margin, and the two come  |
//| apart when the stop is tight: 2% risked over a 1% stop is twice   |
//| equity in notional whatever the instrument. On MBT_X, where       |
//| margin measured 3,400 a lot against a 7,161 notional, the         |
//| risk-correct size needed more margin than the account had. The    |
//| hub could not see that coming -- mirror mode has no margin        |
//| preview, because this bridge could not answer one.                |
//|                                                                   |
//| margin_per_lot is reported alongside the total because margin is  |
//| linear in volume for these instruments, so one call tells the hub |
//| the largest size it can afford instead of it having to ask again  |
//| for every candidate size.                                         |
//|                                                                   |
//| One caveat worth knowing at the other end: OrderCalcMargin prices |
//| a STANDALONE position. It does not model netting against existing |
//| opposite exposure, which on this account released 3,400 a lot     |
//| while a long was reducing a short. The figure is therefore the    |
//| worst case, which is the right direction for a safety check but   |
//| will read high on an account already positioned the other way.    |
//+------------------------------------------------------------------+
string HandleMargin(string json)
  {
   string symbol    = JsonGetString(json, "symbol");
   string direction = JsonGetString(json, "direction");
   double lots      = JsonGetDouble(json, "lots");
   if(lots <= 0.0)
      lots = 1.0;                      // per-lot enquiry

   if(!SymbolSelect(symbol, true))
      return JsonError(StringFormat(
         "symbol '%s' is not offered by this broker, so its margin cannot be "
         "calculated", symbol));

   if(!AwaitPrice(symbol))
      return JsonError(StringFormat(
         "no price for '%s' yet, and margin is priced off the market", symbol));

   bool   isBuy = (direction != "SELL");
   double price = isBuy ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                        : SymbolInfoDouble(symbol, SYMBOL_BID);
   ENUM_ORDER_TYPE otype = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   double margin = 0.0;
   ResetLastError();
   if(!OrderCalcMargin(otype, symbol, lots, price, margin))
      return JsonError(StringFormat(
         "OrderCalcMargin refused %.2f lots of %s at %s (last_error=%d)",
         lots, symbol, DoubleToString(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
         GetLastError()));

   return "{\"status\":\"ok\",\"symbol\":" + JsonQuote(symbol)
        + ",\"lots\":" + DoubleToString(lots, 2)
        + ",\"direction\":" + JsonQuote(isBuy ? "BUY" : "SELL")
        + ",\"price\":" + DoubleToString(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS))
        + ",\"margin\":" + DoubleToString(margin, 2)
        + ",\"margin_per_lot\":" + DoubleToString(margin / lots, 2)
        + ",\"margin_used\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2)
        + ",\"margin_free\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2)
        + ",\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2)
        + ",\"currency\":" + JsonQuote(AccountInfoString(ACCOUNT_CURRENCY))
        + "}";
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

//| Floors to the symbol's volume step, and REFUSES anything outside   |
//| its min/max rather than clamping. Clamping is the tempting choice  |
//| and the wrong one: a master 0.01 against a slave minimum of 0.10   |
//| would silently trade ten times the intended risk. Mirror mode      |
//| copies lots 1:1 with no scaling, so the two brokers' contract      |
//| sizes have to agree for the copy to mean anything anyway.          |
//|                                                                    |
//| 1.03: floors where it used to round to NEAREST. Rounding to        |
//| nearest can round UP, and the hub is explicit that it must not:    |
//| copy_engine._scaled_lots floors to the 0.01 grid because "this     |
//| dial's job is to carry LESS risk than the master, so it must never |
//| hand back more". Those two did not compose. It only bit where the  |
//| step is coarser than 0.01 -- which is not hypothetical: ICM's      |
//| US500 has a 0.1 minimum -- and there a risk_scale of 0.5 on a      |
//| master 0.3 asked for 0.15 and got 0.2, a third more risk than the  |
//| hub had sized, announced only by a Print. Flooring can reach zero  |
//| instead, which then lands on the below-minimum refusal below,      |
//| where an operator sees it.                                         |
double NormalizeVolume(const string symbol, double lots, string &err,
                       bool allowAboveMax = false)
  {
   double vmin = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      step = 0.01;

   // Snap before flooring, or binary representation eats a whole step for
   // volumes that are already exact: 0.03/0.01 is 2.9999999999999996 in
   // doubles, and a bare MathFloor would turn an exact 0.03 into 0.02. The
   // tolerance is a millionth of a step, far below any volume a broker
   // distinguishes, so it can only absorb arithmetic noise.
   double q  = lots / step;
   double qr = MathRound(q);
   double v  = (MathAbs(q - qr) < 1e-6 ? qr : MathFloor(q)) * step;
   v = NormalizeDouble(v, VolumeDigits(step));

   if(MathAbs(v - lots) > 1e-8)
      Print("SlaveBridge: ", symbol, " volume ", lots, " floored to ", v,
            " (step ", step, ")");

   if(v < vmin - 1e-8)
     {
      err = StringFormat("lots %.4f is below %s minimum %.4f -- refusing rather than "
                         "rounding up, which would multiply the intended risk",
                         lots, symbol, vmin);
      return 0.0;
     }
   if(vmax > 0.0 && v > vmax + 1e-8 && !allowAboveMax)
     {
      err = StringFormat("lots %.4f exceeds %s maximum %.4f -- set AllowVolumeSplit "
                         "to open it as several deals held together as one group",
                         lots, symbol, vmax);
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

//| A price on the symbol's TICK grid, not merely rounded to its       |
//| digits. The two are the same thing for a spot pair and are not for  |
//| an exchange future: Darwinex's MBT_X has tick_size 5 and digits 0,  |
//| so NormalizeDouble alone yields a stop like 70949, which sits       |
//| between two tradeable prices and can come back as an invalid price. |
//|                                                                     |
//| Nearest tick, never wider: the master chose the stop, and moving it  |
//| out to fit the grid would change the risk -- the same reason         |
//| CheckStopsDistance refuses rather than adjusts.                     |
double OnTickGrid(const string symbol, double price)
  {
   if(price <= 0.0)
      return 0.0;
   int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double tick   = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick <= 0.0)
      return NormalizeDouble(price, digits);
   return NormalizeDouble(MathRound(price / tick) * tick, digits);
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

//| A JSON string VALUE, quotes included, with the two characters that |
//| would break the hub's json.loads() escaped rather than stripped.   |
//| SYMBOL_PATH is why this exists: every path is backslash-separated  |
//| ("Futures\Energies\WTI"), and one raw backslash makes the whole    |
//| reply unparseable -- which would look exactly like a dead bridge.  |
//| JsonError() mangles instead of escaping, which is right for a      |
//| human-readable message and wrong for a value the hub keys on.      |
string JsonQuote(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   // Broker descriptions occasionally carry a stray control character;
   // a literal newline inside a JSON string is as fatal as a backslash.
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   StringReplace(s, "\t", " ");
   return "\"" + s + "\"";
  }

//| The string entries of a JSON array value, e.g. "symbols":["A","B"].|
//| Returns how many were read, so an absent key and an empty array    |
//| both come back 0 and callers can treat them alike. Non-string      |
//| entries stop the scan: no command needs one, and silently reading  |
//| past a number would be worse than returning the prefix.            |
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

//| True only for the JSON literal true. Absent, false, null, a number |
//| and a string all read as false, which is what every flag wants.    |
bool JsonGetBool(string json, string key)
  {
   int p = FindValueStart(json, key);
   if(p < 0)
      return false;
   return (StringSubstr(json, p, 4) == "true");
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
