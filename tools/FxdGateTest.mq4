//+------------------------------------------------------------------+
//| FxdGateTest.mq4                                                    |
//|                                                                    |
//| THE GO-LIVE GATE. One run, every live strategy, both sides, at     |
//| once -- to prove that what was delivered is what is running.       |
//|                                                                    |
//| The EAs stay attached exactly as built and delivered. This does    |
//| not replace them: it fires a test entry carrying each one's OWN    |
//| magic number, on that unit's own symbol, in the direction that     |
//| unit actually trades. What comes back out the other end is the     |
//| answer: the right slaves took it, at the right size, with the      |
//| right stop, measured on the right timeframe.                       |
//|                                                                    |
//| It answers two questions at once:                                  |
//|                                                                    |
//|   DELIVERY   is every EA attached, enabled, on the right chart,    |
//|              with the magic it was issued? A unit that is silent   |
//|              here is one that would have been silent in the market.|
//|                                                                    |
//|   LATENCY    what happens when twenty of them signal in the same   |
//|              millisecond? Nothing else makes that happen on        |
//|              purpose, and it is the one condition a copier meets   |
//|              on its worst day rather than its average one.         |
//|                                                                    |
//| THE LIST COMES FROM THE REGISTRY. tools/make_gate_list.py writes   |
//| it: magic, symbol, side, timeframe, status -- and it refuses to    |
//| list a unit that is void, rejected or retired, so this cannot fire |
//| a strategy nobody meant to run. Put its file in MQL4/Files.        |
//|                                                                    |
//| THE SIDE IS THE UNIT'S OWN. A `both` unit sends a long AND a short |
//| together, which is also what exercises the copier's refusal of     |
//| opposing legs -- a slave that declines the second one is CORRECT,  |
//| and the run says so rather than counting it as a failure. A        |
//| one-sided unit sends the one side it trades.                       |
//|                                                                    |
//| OPENS FIRST, WAITS ONCE. Every leg is sent before anything sleeps, |
//| so the burst is a burst. A loop that opened-waited-closed per unit |
//| would spread twenty units over several minutes and measure nothing.|
//|                                                                    |
//| DRY RUN BY DEFAULT. It prints what it would send, and the risk     |
//| that adds up to, and stops. Set Fire=true to send.                 |
//|                                                                    |
//| Versions:                                                          |
//|   1.00 2026-11-18 initial build, on FxdMaeEntryTest.mqh 1.06       |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs
#property version "1.00"

//--- The shared body: OpenLeg, DoMaeStep, CloseTicket and the sizing
//--- helpers. MAE_NO_ONSTART leaves its OnStart out, because this file
//--- brings its own. BASE_TF and MagicStart become inputs there and are
//--- NOT used here -- every unit's timeframe and magic come from the
//--- list -- but the include needs them defined.
#define MAE_SCRIPT_VERSION "1.00-GATE"
#define MAE_BASE_TF        PERIOD_D1
#define MAE_MAGIC_START    0
#define MAE_NO_ONSTART
#include "FxdMaeEntryTest.mqh"

//--- the gate's own inputs -------------------------------------------
//--- THE SIMPLE PATH, and the one to reach for when the job is just
//--- "send these legs at once and see what the copier does".
//---
//---     ETHUSD:120002:both,XAGUSD:110001:both
//---
//--- SYMBOL:MAGIC:SIDE, and an optional fourth field for the ATR
//--- timeframe (ETHUSD:120002:both:H4). Without it a leg uses THIS
//--- CHART'S timeframe, which is printed per leg so it is never a
//--- guess. Side is long, short or both; both is the opposing pair,
//--- sent in one burst.
//---
//--- Filled in, this WINS over GateListFile: nothing is read from the
//--- registry, no status is consulted, and sizing comes from this
//--- script's own RISK_A. That is the point -- the registry list
//--- answers "which strategies may fire", which is a different
//--- question from "fire these now".
input string Legs            = "";
input string GateListFile    = "gtc-gate-list.csv";  // in MQL4/Files; see make_gate_list.py
//--- Empty fires every timeframe in the list. "D" or "D,H4" fires those.
//--- The timeframe is the unit's, from the registry -- not this chart's.
input string OnlyTimeframes  = "";
//--- Empty fires every status the list contains. The list itself can
//--- never contain void, rejected or retired.
input string OnlyStatus      = "";
//--- FALSE PRINTS AND SENDS NOTHING. The whole run is visible first:
//--- every leg, its size, and what they add up to.
input bool   Fire            = false;
//--- A ceiling on legs, not a target. Twenty units both-sided is forty
//--- orders in a few milliseconds, and a list that unexpectedly grew is
//--- better stopped than discovered afterwards.
input int    MaxLegs         = 60;

//+------------------------------------------------------------------+
struct GateUnit
  {
   int      magic;
   string   symbol;
   string   side;
   string   timeframe;
   string   status;
   string   source;
  };

//--- Every leg opened by this run, flat. Parallel rather than nested
//--- because closing needs the SYMBOL a ticket belongs to, and MQL4
//--- has no array of structs that survives being passed around.
int      g_ticket[];
string   g_symbol[];
int      g_type[];
int      g_digits[];
double   g_op[];
double   g_oldSl[];
int      g_legs = 0;

//+------------------------------------------------------------------+
ENUM_TIMEFRAMES TfFromCode(string code)
  {
   string c = code;
   StringToUpper(c);
   if(c == "W" || c == "W1")  return(PERIOD_W1);
   if(c == "D" || c == "D1")  return(PERIOD_D1);
   if(c == "H4")              return(PERIOD_H4);
   if(c == "H1")              return(PERIOD_H1);
   if(c == "M30")             return(PERIOD_M30);
   if(c == "M15")             return(PERIOD_M15);
   if(c == "M5")              return(PERIOD_M5);
   if(c == "M1")              return(PERIOD_M1);
   return(PERIOD_CURRENT);
  }

//+------------------------------------------------------------------+
//| The inverse, so a typed leg with no timeframe field can report     |
//| the chart's own period as a word the plan line can print and       |
//| TfFromCode can read straight back.                                 |
//+------------------------------------------------------------------+
string TfCode(ENUM_TIMEFRAMES tf)
  {
   switch(tf)
     {
      case PERIOD_W1:  return("W");
      case PERIOD_D1:  return("D");
      case PERIOD_H4:  return("H4");
      case PERIOD_H1:  return("H1");
      case PERIOD_M30: return("M30");
      case PERIOD_M15: return("M15");
      case PERIOD_M5:  return("M5");
      case PERIOD_M1:  return("M1");
     }
   //--- A period with no code of its own. Named rather than silently
   //--- turned into D1: the ATR timeframe decides the stop distance,
   //--- which decides the size of every leg in the burst.
   return("TF" + IntegerToString((int)tf));
  }

//+------------------------------------------------------------------+
//| Is `value` in a comma list? An empty list means "everything",     |
//| which is not the same as an empty value matching nothing.         |
//+------------------------------------------------------------------+
bool InList(string list, string value)
  {
   string trimmed = list;
   StringTrimLeft(trimmed);
   StringTrimRight(trimmed);
   if(trimmed == "")
      return(true);

   string parts[];
   StringSplitSimple(trimmed, ',', parts);
   string want = value;
   StringToUpper(want);
   for(int i = 0; i < ArraySize(parts); i++)
     {
      string p = parts[i];
      StringTrimLeft(p);
      StringTrimRight(p);
      StringToUpper(p);
      if(p == want)
         return(true);
     }
   return(false);
  }

//+------------------------------------------------------------------+
//| The symbol as THIS terminal knows it.                             |
//|                                                                   |
//| The list carries the registry's tradable name (XAGUSD). A broker   |
//| may add a suffix -- XAGUSD.r, XAGUSD-ECN, XAGUSDm -- and that is   |
//| per terminal, so the registry cannot know it and this has to look. |
//| Returns "" when nothing matches, and the run reports that unit     |
//| rather than sending an order into a symbol that does not exist.    |
//+------------------------------------------------------------------+
string ResolveSymbol(string wanted)
  {
   if(MarketInfo(wanted, MODE_BID) > 0.0)
      return(wanted);

   //--- Market Watch first, then every symbol the server lists: a
   //--- suffixed name is usually already in the window, and scanning
   //--- the full list is the fallback for when it is not.
   for(int pass = 0; pass < 2; pass++)
     {
      int total = SymbolsTotal(pass == 0);
      for(int i = 0; i < total; i++)
        {
         string name = SymbolName(i, pass == 0);
         if(StringFind(name, wanted) == 0 && MarketInfo(name, MODE_BID) > 0.0)
            return(name);
        }
     }
   return("");
  }

//+------------------------------------------------------------------+
//| Legs typed straight into the input, no registry involved.          |
//|                                                                    |
//| SYMBOL:MAGIC:SIDE[:TF], comma separated. A leg that cannot be read  |
//| is NAMED and skipped, like a bad list line -- a burst that silently |
//| lost a leg is the failure this whole script exists to avoid.        |
//|                                                                    |
//| The magic still has to be the EA's own. The script places the       |
//| orders itself and stamps them, so the copier sees exactly what it   |
//| would see from the EA; what this does NOT test is the EA's own      |
//| entry logic or that it is attached at all. `live-set verify` is the |
//| check for that, and it needs no orders.                            |
//+------------------------------------------------------------------+
int ParseLegs(string spec, GateUnit &units[])
  {
   string trimmed = spec;
   StringTrimLeft(trimmed);
   StringTrimRight(trimmed);
   if(trimmed == "")
      return(0);

   string items[];
   StringSplitSimple(trimmed, ',', items);
   int count = 0;

   for(int i = 0; i < ArraySize(items); i++)
     {
      string item = items[i];
      StringTrimLeft(item);
      StringTrimRight(item);
      if(item == "")
         continue;

      string f[];
      StringSplitSimple(item, ':', f);
      if(ArraySize(f) < 3)
        {
         Print("  ! '", item, "' needs SYMBOL:MAGIC:SIDE -- skipped");
         continue;
        }

      GateUnit u;
      u.symbol = f[0];
      u.magic  = (int)StringToInteger(f[1]);
      u.side   = f[2];
      StringTrimLeft(u.symbol); StringTrimRight(u.symbol);
      StringTrimLeft(u.side);   StringTrimRight(u.side);
      StringToLower(u.side);
      //--- No fourth field means this chart's timeframe. TfCode turns it
      //--- back into a word so the plan prints what it actually used.
      u.timeframe = (ArraySize(f) >= 4) ? f[3] : TfCode((ENUM_TIMEFRAMES)Period());
      StringTrimLeft(u.timeframe); StringTrimRight(u.timeframe);
      //--- Typed legs carry no registry status. Named rather than left
      //--- blank so OnlyStatus still behaves predictably if it is set,
      //--- and so nothing downstream has to treat "" as a status.
      u.status = "typed";
      u.source = "typed";

      if(u.magic <= 0)
        {
         Print("  ! '", item, "' has no usable magic -- skipped");
         continue;
        }

      ArrayResize(units, count + 1);
      units[count] = u;
      count++;
     }
   return(count);
  }

//+------------------------------------------------------------------+
//| Read the list. Blank lines and # comments are skipped; a line      |
//| that cannot be read is NAMED and skipped, never guessed at.        |
//+------------------------------------------------------------------+
int ReadGateList(string filename, GateUnit &units[])
  {
   int h = FileOpen(filename, FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
     {
      Print("ERROR: cannot open ", filename, " in MQL4\\Files: ", GetLastError(),
            ". Generate it with tools/make_gate_list.py and copy it there.");
      return(0);
     }

   int count = 0;
   int lineNo = 0;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      lineNo++;
      StringTrimLeft(line);
      StringTrimRight(line);
      if(line == "" || StringGetChar(line, 0) == '#')
         continue;

      string f[];
      StringSplitSimple(line, ',', f);
      if(ArraySize(f) < 5)
        {
         Print("  ! line ", lineNo, " has ", ArraySize(f),
               " field(s), expected at least 5 -- skipped: ", line);
         continue;
        }

      GateUnit u;
      u.magic     = (int)StringToInteger(f[0]);
      u.symbol    = f[1];
      u.side      = f[2];
      u.timeframe = f[3];
      u.status    = f[4];
      u.source    = (ArraySize(f) >= 7) ? f[6] : "";
      StringToLower(u.side);

      if(u.magic <= 0)
        {
         Print("  ! line ", lineNo, " has no usable magic -- skipped: ", line);
         continue;
        }

      ArrayResize(units, count + 1);
      units[count] = u;
      count++;
     }
   FileClose(h);
   return(count);
  }

//+------------------------------------------------------------------+
void RecordLeg(int ticket, string symbol, int type, int digits, double op, double oldSl)
  {
   ArrayResize(g_ticket, g_legs + 1);
   ArrayResize(g_symbol, g_legs + 1);
   ArrayResize(g_type,   g_legs + 1);
   ArrayResize(g_digits, g_legs + 1);
   ArrayResize(g_op,     g_legs + 1);
   ArrayResize(g_oldSl,  g_legs + 1);
   g_ticket[g_legs] = ticket;
   g_symbol[g_legs] = symbol;
   g_type[g_legs]   = type;
   g_digits[g_legs] = digits;
   g_op[g_legs]     = op;
   g_oldSl[g_legs]  = oldSl;
   g_legs++;
  }

//+------------------------------------------------------------------+
void OnStart()
  {
   Print("FxdGateTest ", MAE_SCRIPT_VERSION, " on account ", AccountNumber(),
         " -- ", (Fire ? "FIRING" : "DRY RUN, nothing will be sent"));

   GateUnit units[];
   int found = 0;
   //--- Typed legs win. Said out loud, because running the registry list
   //--- when you meant to fire two legs by hand -- or the reverse -- is
   //--- the one mistake here that costs real money.
   string typed = Legs;
   StringTrimLeft(typed);
   StringTrimRight(typed);
   if(typed != "")
     {
      Print("source: Legs input (typed) -- GateListFile ignored, no registry read");
      found = ParseLegs(typed, units);
      if(found <= 0)
        {
         Print("ERROR: no usable legs in Legs=\"", Legs, "\"");
         return;
        }
     }
   else
     {
      Print("source: ", GateListFile, " (registry list)");
      found = ReadGateList(GateListFile, units);
      if(found <= 0)
        {
         Print("ERROR: no units read from ", GateListFile);
         return;
        }
     }

   //--- plan ------------------------------------------------------------
   //--- Worked out in full BEFORE anything is sent, because the whole
   //--- point of the burst is that it is simultaneous: deciding what to
   //--- send while sending it would put the filtering between the orders.
   int      planMagic[];
   string   planSymbol[];
   bool     planLong[];
   ENUM_TIMEFRAMES planTf[];
   int      plans = 0;
   int      skipped = 0;

   for(int i = 0; i < found; i++)
     {
      if(!InList(OnlyTimeframes, units[i].timeframe))
         continue;
      if(!InList(OnlyStatus, units[i].status))
         continue;

      string resolved = ResolveSymbol(units[i].symbol);
      if(resolved == "")
        {
         Print("  ! magic ", units[i].magic, ": this terminal has no symbol ",
               units[i].symbol, " (nor any name starting with it) -- skipped");
         skipped++;
         continue;
        }

      //--- The unit's own side. `both` is two legs on one symbol at one
      //--- moment, which is the opposing pair.
      bool wantLong  = (units[i].side == "both" || units[i].side == "long");
      bool wantShort = (units[i].side == "both" || units[i].side == "short");
      if(!wantLong && !wantShort)
        {
         Print("  ! magic ", units[i].magic, ": side '", units[i].side,
               "' is not long, short or both -- skipped");
         skipped++;
         continue;
        }

      for(int leg = 0; leg < 2; leg++)
        {
         bool goLong = (leg == 0);
         if(goLong && !wantLong)   continue;
         if(!goLong && !wantShort) continue;

         ArrayResize(planMagic,  plans + 1);
         ArrayResize(planSymbol, plans + 1);
         ArrayResize(planLong,   plans + 1);
         ArrayResize(planTf,     plans + 1);
         planMagic[plans]  = units[i].magic;
         planSymbol[plans] = resolved;
         planLong[plans]   = goLong;
         planTf[plans]     = TfFromCode(units[i].timeframe);
         plans++;
        }
     }

   Print("  ", found, " unit(s) in the list, ", plans, " leg(s) to send",
         (skipped > 0 ? ", " + IntegerToString(skipped) + " skipped" : ""));
   for(int pi = 0; pi < plans; pi++)
      Print("    ", (planLong[pi] ? "LONG  " : "SHORT "), planSymbol[pi],
            "  magic=", planMagic[pi], "  ATR tf=", EnumToString(planTf[pi]));

   if(plans <= 0)
     {
      Print("ERROR: nothing to send after filtering.");
      return;
     }
   if(plans > MaxLegs)
     {
      Print("ERROR: ", plans, " legs is over MaxLegs (", MaxLegs,
            "). Raise it deliberately, or narrow OnlyTimeframes.");
      return;
     }

   //--- Said as a number, because "2% per leg" and forty legs are very
   //--- different sentences and only one of them is on screen.
   if(ACCT_RISK_MODE != 0)
      Print("  intended risk: ", DoubleToStr(RISK_A, 2), "% per leg x ", plans,
            " = ", DoubleToStr(RISK_A * plans, 1), "% of ",
            (ACCT_RISK_MODE == 1 ? "balance" : "equity"),
            " staked at once, before any slave re-sizes it");
   else
      Print("  intended risk: ", FIXED_RISK_A, " per leg x ", plans, " = ",
            DoubleToStr((double)FIXED_RISK_A * plans, 0), " staked at once");

   if(!Fire)
     {
      Print("DRY RUN -- nothing sent. Set Fire=true to send the ", plans, " leg(s) above.");
      return;
     }

   if(!TradingIsAllowed())
      return;

   //--- fire -------------------------------------------------------------
   uint startedAt = GetTickCount();
   for(int si = 0; si < plans; si++)
     {
      int    tickets[];
      double ops[];
      double oldSls[];
      int    type = 0;
      int placed = OpenLeg(planSymbol[si], planMagic[si], planLong[si], planTf[si],
                           tickets, ops, oldSls, type);
      int digits = (int)MarketInfo(planSymbol[si], MODE_DIGITS);
      for(int k = 0; k < placed; k++)
         RecordLeg(tickets[k], planSymbol[si], type, digits, ops[k], oldSls[k]);
     }
   uint burstMs = GetTickCount() - startedAt;

   Print("BURST: ", g_legs, " order(s) from ", plans, " leg(s) in ", burstMs, "ms");
   if(g_legs <= 0)
     {
      Print("ERROR: nothing opened.");
      return;
     }
   if(g_legs < plans)
      Print("  NOTE: ", plans - g_legs, " leg(s) did not open -- see the errors above. ",
            "On a gate run that is the finding, not a nuisance.");

   //--- ONE wait for all of them, then the MAE step. Per leg it would be
   //--- a wait per leg, and the burst would stop being one.
   if(DoMaeModify)
     {
      Sleep(MAE_DELAY_MS);
      for(int mi = 0; mi < g_legs; mi++)
        {
         if(g_oldSl[mi] <= 0.0)
           {
            Print("MAE modify skipped for ticket ", g_ticket[mi], ": no stop to widen.");
            continue;
           }
         DoMaeStep(g_ticket[mi], g_op[mi], g_oldSl[mi], g_type[mi],
                   g_symbol[mi], g_digits[mi]);
        }
     }
   else
      Print("MAE modify skipped (DoMaeModify = false)");

   //--- hold, then close every ticket whatever happened to the others ----
   if(CloseAfterMs > 0)
     {
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
               " -- closing ", g_legs, " ticket(s) anyway rather than leaving them open");
      else
         Print("Held ", g_legs, " ticket(s) for ", waited, "ms; closing now");
      for(int ci = 0; ci < g_legs; ci++)
         CloseTicket(g_ticket[ci], g_symbol[ci], g_digits[ci]);
     }
   else
      Print("Leaving ", g_legs, " ticket(s) open (CloseAfterMs = 0) -- ",
            "these trades and every slave leg they opened are yours to close");

   Print("GATE RUN DONE: ", g_legs, " order(s), burst ", burstMs,
         "ms. Read the copy side on the Copier tab -- per-leg latency and ",
         "slippage are recorded there, and a slave that refused an opposing ",
         "leg shows as 'not copied' on purpose.");
  }
