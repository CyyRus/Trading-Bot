#property copyright "Cyy XAUUSD Survival Scalping Bot"
#property version   "3.8"
#property strict

#include <Trade\Trade.mqh>

//─── Inputs ──────────────────────────────────────────────────────────────────
input double LotSize           = 0.005;   // Lot size
input int    StopLossPips      = 30;      // Stop loss in pips
input double RiskRewardRatio   = 1.8;     // TP = SL * RR
input int    MaxOpenTrades     = 1;       // Max simultaneous positions
input int    CooldownMinutes   = 15;      // Minutes between trades
input double TargetBalance     = 5000.0;  // Stop trading at this balance
input int    MagicNumber       = 12345;

input double MaxSpreadPoints   = 50;      // Max allowed spread (points)
input string AllowedSymbol     = "XAUUSD";
input int    D1TrendMAPeriod   = 50;      // D1 EMA period — daily trend gate
input int    H1TrendMAPeriod   = 50;      // H1 EMA period (info only)
input int    ConfirmCandles    = 2;       // Breakout confirmation candles
input int    PullbackPips      = 10;      // Required pullback depth (pips)
input bool   EnableVolPause    = false;   // Pause on volatile hour edges

// Exit management
input int    BreakevenPips     = 10;   // Move SL to entry once floating profit hits this
input int    TrailStartPips    = 18;   // Begin trailing SL at this profit level
input int    TrailStepPips     = 8;    // Keep SL this many pips behind current price
input int    MomentumExitPips  = 8;    // Early-exit threshold: if losing >= this AND last M5 candle opposes trade, close now

//─── Globals ─────────────────────────────────────────────────────────────────
CTrade   trade;
datetime lastTradeTime  = 0;
datetime lastAlivePrint = 0;
datetime lastDiagBar    = 0;
datetime lastExitBar    = 0;   // tracks last M5 bar for momentum-exit check
int      d1MAHandle     = INVALID_HANDLE;
int      h1MAHandle     = INVALID_HANDLE;

//─── OnInit ──────────────────────────────────────────────────────────────────
int OnInit()
{
   if(AllowedSymbol != "" && _Symbol != AllowedSymbol)
   {
      Print("ERROR: EA designed for ", AllowedSymbol, " but attached to ", _Symbol);
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(MagicNumber);

   d1MAHandle = iMA(_Symbol, PERIOD_D1, D1TrendMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(d1MAHandle == INVALID_HANDLE)
   {
      Print("ERROR: Failed to create D1 MA handle");
      return INIT_FAILED;
   }

   h1MAHandle = iMA(_Symbol, PERIOD_H1, H1TrendMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(h1MAHandle == INVALID_HANDLE)
   {
      Print("ERROR: Failed to create H1 MA handle");
      return INIT_FAILED;
   }

   lastAlivePrint = TimeCurrent();

   double effLot = NormalizeLot(LotSize);
   Print("=================================================================");
   Print("Cyy Scalping Bot v3.8 | Symbol: ", _Symbol);
   Print("Balance    : ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   Print("Equity     : ", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),  2));
   Print("LotSize    : ", DoubleToString(LotSize, 3),
         " -> effective: ", DoubleToString(effLot, 3),
         " (min=", DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), 3),
         " step=", DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP), 3), ")");
   Print("Breakeven  : +", BreakevenPips, " pips | Trail start: +", TrailStartPips,
         " pips | Trail step: ", TrailStepPips, " pips | MomentumExit: -", MomentumExitPips, " pips");
   Print("Vol pause  : ", EnableVolPause ? "ON" : "OFF");
   Print("=================================================================");
   return INIT_SUCCEEDED;
}

//─── OnDeinit ────────────────────────────────────────────────────────────────
void OnDeinit(const int reason)
{
   if(d1MAHandle != INVALID_HANDLE) IndicatorRelease(d1MAHandle);
   if(h1MAHandle != INVALID_HANDLE) IndicatorRelease(h1MAHandle);
   Print("Bot stopped | Reason: ", reason,
         " | Balance: ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
}

//─── Helpers ─────────────────────────────────────────────────────────────────
int CountOpenTrades()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetTicket(i) > 0 &&
         PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         count++;
   }
   return count;
}

bool IsCooldownActive()
{
   if(lastTradeTime == 0) return false;
   int secondsLeft = (CooldownMinutes * 60) - (int)(TimeCurrent() - lastTradeTime);
   if(secondsLeft > 0)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Cooldown: ", secondsLeft, "s remaining");
         lastLog = TimeCurrent();
      }
      return true;
   }
   return false;
}

bool IsVolatileTime()
{
   if(!EnableVolPause) return false;
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   if(tm.min < 2 || tm.min > 58)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Volatile hour edge — pausing");
         lastLog = TimeCurrent();
      }
      return true;
   }
   return false;
}

bool IsSpreadAcceptable()
{
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point == 0) return false;
   double spreadPoints = (SymbolInfoDouble(_Symbol, SYMBOL_ASK) -
                          SymbolInfoDouble(_Symbol, SYMBOL_BID)) / point;
   if(spreadPoints > MaxSpreadPoints)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Spread too wide: ", DoubleToString(spreadPoints, 1), " pts");
         lastLog = TimeCurrent();
      }
      return false;
   }
   return true;
}

double NormalizeLot(double desiredLot)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(stepLot <= 0) stepLot = 0.01;

   double lot = MathFloor(desiredLot / stepLot) * stepLot;
   lot = MathMax(minLot, MathMin(maxLot, lot));
   return NormalizeDouble(lot, 2);
}

//─── Trade management (called every tick while a position is open) ────────────
// Three layers:
//   1. Breakeven : once price moves BreakevenPips in our favour, lock SL at entry.
//   2. Trail     : once TrailStartPips in profit, keep SL TrailStepPips behind price.
//   3. Momentum exit : once per new M5 candle — if we are losing >= MomentumExitPips
//                      AND the last closed candle opposes the trade, close early.
void ManageOpenTrades()
{
   double pip = SymbolInfoDouble(_Symbol, SYMBOL_POINT) * 10;
   bool   newBar = false;
   datetime barTime = iTime(_Symbol, PERIOD_M5, 1);
   if(barTime != lastExitBar) { lastExitBar = barTime; newBar = true; }

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)    continue;

      long   posType = PositionGetInteger(POSITION_TYPE);
      double entry   = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl      = PositionGetDouble(POSITION_SL);
      double tp      = PositionGetDouble(POSITION_TP);
      double bid     = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double ask     = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

      // floating P&L in pips (positive = profit)
      double floatPips = (posType == POSITION_TYPE_BUY)
                         ? (bid - entry) / pip
                         : (entry - ask) / pip;

      // ── Layer 1: Breakeven ───────────────────────────────────────────────
      if(floatPips >= BreakevenPips)
      {
         double beSL = (posType == POSITION_TYPE_BUY)
                       ? NormalizeDouble(entry + pip, _Digits)   // 1 pip above entry
                       : NormalizeDouble(entry - pip, _Digits);  // 1 pip below entry

         bool needsMove = (posType == POSITION_TYPE_BUY  && (sl == 0 || sl < entry))
                       || (posType == POSITION_TYPE_SELL && (sl == 0 || sl > entry));
         if(needsMove)
         {
            if(trade.PositionModify(ticket, beSL, tp))
               Print("Breakeven locked | ticket: ", ticket,
                     " | SL: ", sl, " -> ", beSL,
                     " | float: +", DoubleToString(floatPips, 1), " pips");
            else
               Print("Breakeven modify failed | error: ", GetLastError());
         }
      }

      // ── Layer 2: Trailing stop ───────────────────────────────────────────
      if(floatPips >= TrailStartPips)
      {
         double trailDist = TrailStepPips * pip;
         double newSL     = (posType == POSITION_TYPE_BUY)
                            ? NormalizeDouble(bid - trailDist, _Digits)
                            : NormalizeDouble(ask + trailDist, _Digits);

         bool improves = (posType == POSITION_TYPE_BUY  && newSL > sl)
                      || (posType == POSITION_TYPE_SELL && (sl == 0 || newSL < sl));
         if(improves)
         {
            if(trade.PositionModify(ticket, newSL, tp))
               Print("Trail moved | ticket: ", ticket,
                     " | SL: ", sl, " -> ", newSL,
                     " | float: +", DoubleToString(floatPips, 1), " pips");
            else
               Print("Trail modify failed | error: ", GetLastError());
         }
      }

      // ── Layer 3: Momentum exit (once per candle) ─────────────────────────
      // If we are losing >= MomentumExitPips AND the last closed M5 candle
      // moves further against us, don't wait for the full SL — exit now.
      if(newBar && floatPips <= -MomentumExitPips)
      {
         double cClose = iClose(_Symbol, PERIOD_M5, 1);
         double cOpen  = iOpen (_Symbol, PERIOD_M5, 1);
         bool opposes  = (posType == POSITION_TYPE_BUY  && cClose < cOpen)   // bearish candle on a buy
                      || (posType == POSITION_TYPE_SELL && cClose > cOpen);   // bullish candle on a sell

         if(opposes)
         {
            if(trade.PositionClose(ticket))
               Print("Momentum exit | ticket: ", ticket,
                     " | float: ", DoubleToString(floatPips, 1), " pips",
                     " | candle opposed trade direction");
            else
               Print("Momentum exit failed | ticket: ", ticket, " | error: ", GetLastError());
         }
      }
   }
}

//─── Strategy ────────────────────────────────────────────────────────────────
int GetBreakoutSignal()
{
   int bulls = 0, bears = 0;
   int startIdx = 2;
   int endIdx   = startIdx + ConfirmCandles - 1;

   for(int i = startIdx; i <= endIdx; i++)
   {
      double o = iOpen (_Symbol, PERIOD_M5, i);
      double c = iClose(_Symbol, PERIOD_M5, i);
      if(c > o) bulls++;
      else if(c < o) bears++;
   }

   if(bulls == ConfirmCandles) return  1;
   if(bears == ConfirmCandles) return -1;
   return 0;
}

int GetD1TrendDirection()
{
   double ma[];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(d1MAHandle, 0, 0, 1, ma) <= 0) return 0;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid > ma[0]) return  1;
   if(bid < ma[0]) return -1;
   return 0;
}

int GetH1TrendDirection()
{
   double ma[];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(h1MAHandle, 0, 0, 1, ma) <= 0) return 0;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid > ma[0]) return  1;
   if(bid < ma[0]) return -1;
   return 0;
}

bool IsPullbackAfterBreakout(int signal)
{
   double pip           = SymbolInfoDouble(_Symbol, SYMBOL_POINT) * 10;
   double close1        = iClose(_Symbol, PERIOD_M5, 1);
   double closeBreakout = iClose(_Symbol, PERIOD_M5, 2);

   if(signal ==  1) return (closeBreakout - close1) > PullbackPips * pip;
   if(signal == -1) return (close1 - closeBreakout) > PullbackPips * pip;
   return false;
}

//─── OnTick ──────────────────────────────────────────────────────────────────
void OnTick()
{
   // Hourly heartbeat
   datetime now = TimeCurrent();
   if(now - lastAlivePrint >= 3600)
   {
      Print("Alive | Balance: ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
            " | Equity: ",       DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),  2),
            " | Open: ",         CountOpenTrades());
      lastAlivePrint = now;
   }

   // Target check
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance >= TargetBalance)
   {
      Print("TARGET REACHED | Balance: ", DoubleToString(balance, 2));
      ExpertRemove();
      return;
   }

   // Manage any open positions first (runs every tick)
   ManageOpenTrades();

   // Pre-trade guards
   if(CountOpenTrades()  >= MaxOpenTrades) return;
   if(IsVolatileTime())                    return;
   if(IsCooldownActive())                  return;
   if(!IsSpreadAcceptable())               return;

   // Signal pipeline — with per-candle diagnostic logging
   int signal   = GetBreakoutSignal();
   int d1Trend  = GetD1TrendDirection();
   int h1Trend  = GetH1TrendDirection();
   bool pullback = (signal != 0) && IsPullbackAfterBreakout(signal);

   datetime barTime = iTime(_Symbol, PERIOD_M5, 1);
   if(barTime != lastDiagBar)
   {
      lastDiagBar = barTime;
      string why = "WAITING";
      if     (signal == 0)       why = "No breakout (candles mixed)";
      else if(d1Trend != signal) why = "D1 trend blocks trade (breakout=" + (string)signal + " D1=" + (string)d1Trend + ")";
      else if(!pullback)         why = "Pullback too small (<" + (string)PullbackPips + " pips)";
      else                       why = "ALL CLEAR — placing trade";
      Print("Diag | breakout=", signal, " D1=", d1Trend, " H1=", h1Trend, "(info) pullback=", pullback, " | ", why);
   }

   if(signal == 0)         return;
   if(d1Trend != signal)   return;
   if(!pullback)           return;

   // Calculate SL / TP
   double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point == 0) return;
   double slDist = StopLossPips                   * point * 10;
   double tpDist = StopLossPips * RiskRewardRatio * point * 10;

   double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double effLot = NormalizeLot(LotSize);
   bool   ok     = false;

   if(signal == 1)
   {
      double sl = NormalizeDouble(ask - slDist, _Digits);
      double tp = NormalizeDouble(ask + tpDist, _Digits);
      ok = trade.Buy(effLot, _Symbol, ask, sl, tp, "Pullback Buy");
      if(ok)
         Print("BUY  | lot: ", effLot, " | entry: ", ask, " | SL: ", sl, " | TP: ", tp);
      else
         Print("BUY failed | lot: ", effLot, " | error: ", GetLastError());
   }
   else
   {
      double sl = NormalizeDouble(bid + slDist, _Digits);
      double tp = NormalizeDouble(bid - tpDist, _Digits);
      ok = trade.Sell(effLot, _Symbol, bid, sl, tp, "Pullback Sell");
      if(ok)
         Print("SELL | lot: ", effLot, " | entry: ", bid, " | SL: ", sl, " | TP: ", tp);
      else
         Print("SELL failed | lot: ", effLot, " | error: ", GetLastError());
   }

   if(ok) lastTradeTime = TimeCurrent();
}
