#property copyright "Cyy XAUUSD Scalp-Hedge Hybrid Bot"
#property version   "1.0"
#property strict
#include <Trade\Trade.mqh>

//─── Inputs ───────────────────────────────────────────────────────────────────
input group "=== Identity ==="
input int    MagicNumber          = 22222;
input string AllowedSymbol        = "XAUUSD";

input group "=== Scalp Settings ==="
input double LotSize              = 0.01;   // Base lot
input int    ScalpTPPips          = 20;     // Scalp TP (pips)
input int    ScalpSLPips          = 40;     // Hard SL if hedge disabled
input int    BreakevenPips        = 10;     // Move SL to entry at this profit
input int    TrailStartPips       = 15;     // Start trailing at this profit
input int    TrailStepPips        = 8;      // Trail distance behind price

input group "=== Hedge Settings ==="
input bool   EnableHedge          = true;   // Enable hedging recovery mode
input int    HedgeTriggerPips     = 20;     // Loss pips to trigger hedge
input double HedgeLotMultiplier   = 1.5;    // Hedge lot = LotSize * this
input int    HedgeTPPips          = 30;     // Hedge TP (pips)
input double NetCloseProfitUSD    = 2.0;    // Close both when combined P&L >= this $
input double NetCloseMaxLossUSD   = -30.0;  // Emergency close both if loss exceeds this $

input group "=== Entry Filters ==="
input int    D1TrendMAPeriod      = 50;
input double MaxSpreadPoints      = 80.0;
input int    CooldownSeconds      = 180;    // Cooldown after full cycle closes

input group "=== Session ==="
input bool   EnableSessionFilter  = true;
input int    SessionStartHour     = 8;      // UTC
input int    SessionEndHour       = 22;     // UTC

input group "=== Safety ==="
input double MaxDrawdownPercent   = 25.0;   // Stop EA if equity drops this %
input double TargetBalanceUSD     = 0.0;    // Auto-stop at this balance (0 = disabled)

//─── Globals ──────────────────────────────────────────────────────────────────
CTrade   trade;

// State machine
enum EBotState { STATE_IDLE, STATE_SCALPING, STATE_HEDGING };
EBotState botState = STATE_IDLE;

ulong    scalpTicket   = 0;
ulong    hedgeTicket   = 0;
int      scalpDir      = 0;   // 1=BUY -1=SELL
datetime lastCycleEnd  = 0;
datetime lastAlivePrint = 0;
datetime lastExitBar   = 0;

int      d1MAHandle    = INVALID_HANDLE;

//─── OnInit ───────────────────────────────────────────────────────────────────
int OnInit()
{
   if(AllowedSymbol != "" && _Symbol != AllowedSymbol)
   {
      Print("ERROR: Wrong symbol — expected ", AllowedSymbol);
      return INIT_FAILED;
   }

   // Hedging accounts only — check margin mode
   if(EnableHedge && (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE)
      != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
   {
      Print("WARNING: Account is not in hedging mode. Hedge will open but may net out.");
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetTypeFillingBySymbol(_Symbol);

   d1MAHandle = iMA(_Symbol, PERIOD_D1, D1TrendMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(d1MAHandle == INVALID_HANDLE)
   {
      Print("ERROR: Failed to create D1 MA handle");
      return INIT_FAILED;
   }

   lastAlivePrint = TimeCurrent();

   Print("=================================================================");
   Print("Cyy ScalpHedge Bot v1.0 | Symbol: ", _Symbol);
   Print("Scalp TP: ", ScalpTPPips, "p | SL: ", ScalpSLPips, "p");
   Print("Hedge: ", EnableHedge ? "ON" : "OFF",
         " | Trigger: -", HedgeTriggerPips, "p | LotMult: x", HedgeLotMultiplier);
   Print("Net exit: +$", NetCloseProfitUSD, " | Emergency: $", NetCloseMaxLossUSD);
   Print("=================================================================");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(d1MAHandle != INVALID_HANDLE) IndicatorRelease(d1MAHandle);
   Print("Bot stopped | Reason: ", reason);
}

//─── Helpers ──────────────────────────────────────────────────────────────────
double Pip() { return SymbolInfoDouble(_Symbol, SYMBOL_POINT) * 10.0; }

double NormalizeLot(double lot)
{
   double minL  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxL  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0) step = 0.01;
   lot = MathFloor(lot / step) * step;
   return NormalizeDouble(MathMax(minL, MathMin(maxL, lot)), 2);
}

bool PositionExists(ulong ticket)
{
   if(ticket == 0) return false;
   return PositionSelectByTicket(ticket);
}

// Net P&L of scalp + hedge combined (unrealised)
double GetNetPnL()
{
   double pnl = 0;
   if(PositionExists(scalpTicket))
      pnl += PositionGetDouble(POSITION_PROFIT)
           + PositionGetDouble(POSITION_SWAP);
   if(PositionExists(hedgeTicket))
      pnl += PositionGetDouble(POSITION_PROFIT)
           + PositionGetDouble(POSITION_SWAP);
   return pnl;
}

double GetFloatPips(ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return 0;
   long   ptype = PositionGetInteger(POSITION_TYPE);
   double entry = PositionGetDouble(POSITION_PRICE_OPEN);
   double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask   = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   return (ptype == POSITION_TYPE_BUY)
          ? (bid - entry) / Pip()
          : (entry - ask) / Pip();
}

bool IsSpreadOK()
{
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point < 1e-10) return false;
   double spread = (SymbolInfoDouble(_Symbol, SYMBOL_ASK) -
                    SymbolInfoDouble(_Symbol, SYMBOL_BID)) / point;
   return spread <= MaxSpreadPoints;
}

bool IsWithinSession()
{
   if(!EnableSessionFilter) return true;
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   return (tm.hour >= SessionStartHour && tm.hour < SessionEndHour);
}

bool IsCooldownActive()
{
   if(lastCycleEnd == 0) return false;
   return (TimeCurrent() - lastCycleEnd) < CooldownSeconds;
}

void CheckSafetyLimits()
{
   // Max drawdown
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   if(balance > 0 && (balance - equity) / balance * 100.0 > MaxDrawdownPercent)
   {
      Print("SAFETY: Max drawdown hit — closing all and stopping");
      CloseAll("MaxDrawdown");
      ExpertRemove();
      return;
   }
   // Target balance
   if(TargetBalanceUSD > 0 && balance >= TargetBalanceUSD)
   {
      Print("SAFETY: Target balance reached — stopping");
      ExpertRemove();
   }
}

//─── Signal functions ─────────────────────────────────────────────────────────
int GetD1Trend()
{
   double ma[1];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(d1MAHandle, 0, 0, 1, ma) != 1) return 0;
   double close = iClose(_Symbol, PERIOD_D1, 1);
   return (close > ma[0]) ? 1 : (close < ma[0]) ? -1 : 0;
}

// Two consecutive M5 candles in same direction = momentum
int GetM5Momentum()
{
   double c1 = iClose(_Symbol, PERIOD_M5, 1), o1 = iOpen(_Symbol, PERIOD_M5, 1);
   double c2 = iClose(_Symbol, PERIOD_M5, 2), o2 = iOpen(_Symbol, PERIOD_M5, 2);
   if(c1 > o1 && c2 > o2) return  1;
   if(c1 < o1 && c2 < o2) return -1;
   return 0;
}

//─── Open Positions ───────────────────────────────────────────────────────────
bool OpenScalp(int dir)
{
   double pip    = Pip();
   double slDist = ScalpSLPips * pip;
   double tpDist = ScalpTPPips * pip;
   double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double lot    = NormalizeLot(LotSize);

   bool ok = false;
   if(dir == 1)
   {
      double sl = EnableHedge ? 0 : NormalizeDouble(ask - slDist, _Digits); // No hard SL if hedging
      double tp = NormalizeDouble(ask + tpDist, _Digits);
      ok = trade.Buy(lot, _Symbol, 0, sl, tp, "Scalp-Buy");
      if(ok) scalpTicket = trade.ResultOrder();
   }
   else
   {
      double sl = EnableHedge ? 0 : NormalizeDouble(bid + slDist, _Digits);
      double tp = NormalizeDouble(bid - tpDist, _Digits);
      ok = trade.Sell(lot, _Symbol, 0, sl, tp, "Scalp-Sell");
      if(ok) scalpTicket = trade.ResultOrder();
   }

   if(ok)
   {
      scalpDir = dir;
      Print("SCALP ", dir == 1 ? "BUY" : "SELL",
            " | Lot:", lot, " | Ticket:", scalpTicket,
            " | TP:", ScalpTPPips, "p",
            EnableHedge ? " | No SL (hedge guards)" : "");
   }
   else
      Print("SCALP open FAILED | err:", GetLastError());

   return ok;
}

bool OpenHedge(int dir)
{
   double pip    = Pip();
   double tpDist = HedgeTPPips * pip;
   double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double lot    = NormalizeLot(LotSize * HedgeLotMultiplier);

   bool ok = false;
   if(dir == 1)
   {
      double tp = NormalizeDouble(ask + tpDist, _Digits);
      ok = trade.Buy(lot, _Symbol, 0, 0, tp, "Hedge-Buy");
      if(ok) hedgeTicket = trade.ResultOrder();
   }
   else
   {
      double tp = NormalizeDouble(bid - tpDist, _Digits);
      ok = trade.Sell(lot, _Symbol, 0, 0, tp, "Hedge-Sell");
      if(ok) hedgeTicket = trade.ResultOrder();
   }

   if(ok)
      Print("HEDGE ", dir == 1 ? "BUY" : "SELL",
            " | Lot:", lot, " | Ticket:", hedgeTicket,
            " | TP:", HedgeTPPips, "p");
   else
      Print("HEDGE open FAILED | err:", GetLastError());

   return ok;
}

//─── Close helpers ────────────────────────────────────────────────────────────
void ClosePosition(ulong ticket, string reason)
{
   if(!PositionExists(ticket)) return;
   double pnl = PositionGetDouble(POSITION_PROFIT);
   if(trade.PositionClose(ticket))
      Print("Closed #", ticket, " | reason: ", reason,
            " | P&L: ", DoubleToString(pnl, 2));
   else
      Print("Close FAILED #", ticket, " | err:", GetLastError());
}

void CloseAll(string reason)
{
   ClosePosition(scalpTicket, reason);
   ClosePosition(hedgeTicket, reason);
   ResetState();
}

void ResetState()
{
   scalpTicket  = 0;
   hedgeTicket  = 0;
   scalpDir     = 0;
   botState     = STATE_IDLE;
   lastCycleEnd = TimeCurrent();
}

//─── Trade Management ─────────────────────────────────────────────────────────
void ManageScalp()
{
   if(!PositionExists(scalpTicket))
   {
      // Scalp closed on its own (TP or SL hit)
      Print("Scalp #", scalpTicket, " closed externally — cycle done");
      if(PositionExists(hedgeTicket)) ClosePosition(hedgeTicket, "ScalpClosed");
      ResetState();
      return;
   }

   double floatPips = GetFloatPips(scalpTicket);
   double sl        = PositionGetDouble(POSITION_SL);
   double tp        = PositionGetDouble(POSITION_TP);
   double pip       = Pip();
   double bid       = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask       = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   long   ptype     = PositionGetInteger(POSITION_TYPE);
   double entry     = PositionGetDouble(POSITION_PRICE_OPEN);

   // Breakeven
   if(floatPips >= BreakevenPips)
   {
      double beSL = (ptype == POSITION_TYPE_BUY)
                    ? NormalizeDouble(entry + pip, _Digits)
                    : NormalizeDouble(entry - pip, _Digits);
      bool needsMove = (ptype == POSITION_TYPE_BUY  && (sl < 1e-10 || sl < entry))
                    || (ptype == POSITION_TYPE_SELL && (sl < 1e-10 || sl > entry));
      if(needsMove)
      {
         if(trade.PositionModify(scalpTicket, beSL, tp))
            Print("Breakeven #", scalpTicket, " | float:+", DoubleToString(floatPips, 1), "p");
      }
   }

   // Trailing stop
   if(floatPips >= TrailStartPips)
   {
      double trailDist = TrailStepPips * pip;
      double newSL     = (ptype == POSITION_TYPE_BUY)
                         ? NormalizeDouble(bid - trailDist, _Digits)
                         : NormalizeDouble(ask + trailDist, _Digits);
      bool improves = (ptype == POSITION_TYPE_BUY  && (sl < 1e-10 || newSL > sl))
                   || (ptype == POSITION_TYPE_SELL && (sl < 1e-10 || newSL < sl));
      if(improves)
      {
         if(trade.PositionModify(scalpTicket, newSL, tp))
            Print("Trail #", scalpTicket, " SL->", newSL,
                  " | float:+", DoubleToString(floatPips, 1), "p");
      }
   }

   // Hedge trigger — open opposite trade when scalp is losing enough
   if(EnableHedge && floatPips <= -HedgeTriggerPips)
   {
      int hedgeDir = -scalpDir;
      Print("HEDGE TRIGGERED | Scalp float: ", DoubleToString(floatPips, 1), "p");
      if(OpenHedge(hedgeDir))
         botState = STATE_HEDGING;
   }
}

void ManageHedge()
{
   bool scalpAlive = PositionExists(scalpTicket);
   bool hedgeAlive = PositionExists(hedgeTicket);

   // If one leg closed on its own (TP hit), close the other
   if(!scalpAlive && hedgeAlive)
   {
      Print("Scalp leg closed (TP?) — closing hedge leg");
      ClosePosition(hedgeTicket, "ScalpTPHit");
      ResetState();
      return;
   }
   if(scalpAlive && !hedgeAlive)
   {
      Print("Hedge leg closed (TP?) — closing scalp leg");
      ClosePosition(scalpTicket, "HedgeTPHit");
      ResetState();
      return;
   }
   if(!scalpAlive && !hedgeAlive)
   {
      Print("Both legs closed externally");
      ResetState();
      return;
   }

   // Both alive — check combined net P&L
   double netPnL = GetNetPnL();

   // Net profit reached — exit both cleanly
   if(netPnL >= NetCloseProfitUSD)
   {
      Print("NET PROFIT TARGET HIT | Combined P&L: +$", DoubleToString(netPnL, 2),
            " — closing both");
      CloseAll("NetProfit");
      return;
   }

   // Emergency net loss limit
   if(netPnL <= NetCloseMaxLossUSD)
   {
      Print("NET LOSS LIMIT HIT | Combined P&L: $", DoubleToString(netPnL, 2),
            " — emergency close");
      CloseAll("NetLossLimit");
      return;
   }

   // Log status periodically
   static datetime lastHedgeLog = 0;
   if(TimeCurrent() - lastHedgeLog >= 30)
   {
      lastHedgeLog = TimeCurrent();
      Print("HEDGE active | Scalp float:", DoubleToString(GetFloatPips(scalpTicket), 1),
            "p | Hedge float:", DoubleToString(GetFloatPips(hedgeTicket), 1),
            "p | Net P&L: $", DoubleToString(netPnL, 2));
   }
}

//─── OnTick ───────────────────────────────────────────────────────────────────
void OnTick()
{
   datetime now = TimeCurrent();

   // Heartbeat
   if(now - lastAlivePrint >= 3600)
   {
      Print("Alive | State:", EnumToString(botState),
            " | Bal:", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
            " | Eq:", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2));
      lastAlivePrint = now;
   }

   CheckSafetyLimits();

   // ── State: SCALPING ──────────────────────────────────────────────────────
   if(botState == STATE_SCALPING)
   {
      ManageScalp();
      return;
   }

   // ── State: HEDGING ───────────────────────────────────────────────────────
   if(botState == STATE_HEDGING)
   {
      ManageHedge();
      return;
   }

   // ── State: IDLE — look for entry ──────────────────────────────────────────
   if(!IsWithinSession())  return;
   if(IsCooldownActive())  return;
   if(!IsSpreadOK())       return;

   int d1Trend = GetD1Trend();
   if(d1Trend == 0) return;

   int m5Mom = GetM5Momentum();
   if(m5Mom == 0 || m5Mom != d1Trend) return;

   // New M5 bar only (avoid multiple triggers per bar)
   datetime barTime = iTime(_Symbol, PERIOD_M5, 1);
   if(barTime == lastExitBar) return;
   lastExitBar = barTime;

   Print("Entry signal | D1:", d1Trend, " M5:", m5Mom, " — opening scalp");
   if(OpenScalp(d1Trend))
      botState = STATE_SCALPING;
}
