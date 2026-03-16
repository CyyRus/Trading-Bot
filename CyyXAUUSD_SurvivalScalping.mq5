#property copyright "Cyy XAUUSD Survival Scalping Bot - Fixed No Pause Spam"
#property version   "3.3"
#property strict
#include <Trade\Trade.mqh>
// ─── Trade Settings ───
input double LotSize = 0.005;                   // Safe for small account
input int StopLossPips = 55;
input double RiskRewardRatio = 1.8;             // TP = SL * 1.8
input int MaxOpenTrades = 1;
input int CooldownMinutes = 15;                 // Shorter cooldown
input double TargetBalance = 5000.0;
input int MagicNumber = 12345;
// ─── Filters ───
input double MaxSpreadPoints = 50;
input string AllowedSymbol = "XAUUSD";
input int H1TrendMAPeriod = 50;                 // H1 MA for trend direction
input int ConfirmCandles = 2;
input int PullbackPips = 25;                    // Wait for pullback after breakout
input bool EnableVolPause = false;              // TURNED OFF by default - no more spam
// ─── Internals ───
CTrade trade;
datetime lastTradeTime = 0;
int h1MAHandle = INVALID_HANDLE;
datetime lastAlivePrint = 0;                    // FIX: now used for periodic heartbeat
//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   if(AllowedSymbol != "" && _Symbol != AllowedSymbol)
   {
      Print("ERROR: EA designed for ", AllowedSymbol, " but attached to ", _Symbol);
      return(INIT_FAILED);
   }
   trade.SetExpertMagicNumber(MagicNumber);
   h1MAHandle = iMA(_Symbol, PERIOD_H1, H1TrendMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(h1MAHandle == INVALID_HANDLE)
   {
      Print("ERROR: Failed to create H1 MA handle");
      return(INIT_FAILED);
   }
   Print("=================================================================");
   Print("Bot v3.3 LOADED - Pullback logic fixed");
   Print("Balance: ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   Print("Equity:  ", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2));
   Print("Vol pause: ", EnableVolPause ? "ON" : "OFF (disabled for trading)");
   Print("=================================================================");
   return(INIT_SUCCEEDED);
}
//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(h1MAHandle != INVALID_HANDLE) IndicatorRelease(h1MAHandle);
   Print("Bot stopped | Reason: ", reason, " | Balance: ", AccountInfoDouble(ACCOUNT_BALANCE));
}
//+------------------------------------------------------------------+
//| Count open trades                                                |
//+------------------------------------------------------------------+
int CountOpenTrades()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         count++;
   }
   return count;
}
//+------------------------------------------------------------------+
//| Cooldown check                                                   |
//+------------------------------------------------------------------+
bool IsCooldownActive()
{
   if(lastTradeTime == 0) return false;
   datetime currentTime = TimeCurrent();
   int secondsLeft = (CooldownMinutes * 60) - (int)(currentTime - lastTradeTime);
   if(secondsLeft > 0)
   {
      static datetime lastLog = 0;
      if(currentTime - lastLog >= 60)
      {
         Print("Cooldown: ", secondsLeft, "s remaining");
         lastLog = currentTime;
      }
      return true;
   }
   return false;
}
//+------------------------------------------------------------------+
//| Check if in narrow volatile edge of hour (only if enabled)       |
//+------------------------------------------------------------------+
bool IsVolatileTime()
{
   if(!EnableVolPause) return false;
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   int min = tm.min;
   if(min < 2 || min > 58)
   {
      static datetime lastPauseLog = 0;
      if(TimeCurrent() - lastPauseLog >= 60)
      {
         Print("Narrow volatile edge of hour - short pause");
         lastPauseLog = TimeCurrent();
      }
      return true;
   }
   return false;
}
//+------------------------------------------------------------------+
//| Spread check                                                     |
//+------------------------------------------------------------------+
bool IsSpreadAcceptable()
{
   double spread = SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point == 0) return false;
   double spreadPoints = spread / point;
   if(spreadPoints > MaxSpreadPoints)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Spread too wide: ", DoubleToString(spreadPoints,1), " points");
         lastLog = TimeCurrent();
      }
      return false;
   }
   return true;
}
//+------------------------------------------------------------------+
//| Get breakout signal from older candles (beyond pullback zone)    |
//| FIX: candles start at index 2 so pullback candle (index 1) is   |
//| a separate, newer candle — breakout and pullback no longer clash |
//+------------------------------------------------------------------+
int GetBreakoutSignal()
{
   int bullCount = 0;
   int bearCount = 0;
   // Start from index 2 so candle 1 remains the pullback candle
   int startIdx = 2;
   int endIdx   = startIdx + ConfirmCandles - 1;
   for(int i = startIdx; i <= endIdx; i++)
   {
      double open  = iOpen (_Symbol, PERIOD_M5, i);
      double close = iClose(_Symbol, PERIOD_M5, i);
      if(close > open) bullCount++;
      else if(close < open) bearCount++;
   }
   if(bullCount == ConfirmCandles) return  1;
   if(bearCount == ConfirmCandles) return -1;
   return 0;
}
//+------------------------------------------------------------------+
//| Get H1 trend direction                                           |
//+------------------------------------------------------------------+
int GetH1TrendDirection()
{
   double maValue[];
   ArraySetAsSeries(maValue, true);
   if(CopyBuffer(h1MAHandle, 0, 0, 1, maValue) <= 0) return 0;
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid > maValue[0]) return  1;
   if(bid < maValue[0]) return -1;
   return 0;
}
//+------------------------------------------------------------------+
//| Check pullback after breakout                                    |
//| FIX: compares candle 1 (most recent) against the last breakout  |
//| candle so breakout and pullback are on different candles         |
//+------------------------------------------------------------------+
bool IsPullbackAfterBreakout(int signal)
{
   double pip    = SymbolInfoDouble(_Symbol, SYMBOL_POINT) * 10;
   double close1 = iClose(_Symbol, PERIOD_M5, 1);              // newest candle
   double closeBreakout = iClose(_Symbol, PERIOD_M5, 2);       // last breakout candle
   if(signal ==  1 && (closeBreakout - close1) > PullbackPips * pip) return true;
   if(signal == -1 && (close1 - closeBreakout) > PullbackPips * pip) return true;
   return false;
}
//+------------------------------------------------------------------+
//| OnTick                                                           |
//+------------------------------------------------------------------+
void OnTick()
{
   // FIX: periodic heartbeat using lastAlivePrint (was declared but never used)
   datetime now = TimeCurrent();
   if(now - lastAlivePrint >= 3600)
   {
      Print("Alive | Balance: ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
            " | Equity: ", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
            " | Open trades: ", CountOpenTrades());
      lastAlivePrint = now;
   }

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance >= TargetBalance)
   {
      Print("TARGET REACHED! Balance: ", balance);
      ExpertRemove();
      return;
   }
   int openTrades = CountOpenTrades();
   if(openTrades >= MaxOpenTrades) return;
   if(IsVolatileTime())    return;
   if(IsCooldownActive())  return;
   if(!IsSpreadAcceptable()) return;
   int breakoutSignal = GetBreakoutSignal();
   if(breakoutSignal == 0) return;
   int h1Trend = GetH1TrendDirection();
   if(h1Trend == 0 || breakoutSignal != h1Trend) return;
   if(!IsPullbackAfterBreakout(breakoutSignal)) return;
   double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask   = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point == 0) return;
   double slDistance = StopLossPips           * point * 10;
   double tpDistance = StopLossPips * RiskRewardRatio * point * 10;
   bool result = false;
   if(breakoutSignal == 1)
   {
      double sl = NormalizeDouble(ask - slDistance, _Digits);
      double tp = NormalizeDouble(ask + tpDistance, _Digits);
      result = trade.Buy(LotSize, _Symbol, ask, sl, tp, "Pullback Buy");
      if(result)
         Print("Pullback BUY  | SL: ", DoubleToString(slDistance / point, 1),
               " pips | TP: ", DoubleToString(tpDistance / point, 1), " pips");
      else
         Print("BUY failed: ", GetLastError());
   }
   else if(breakoutSignal == -1)
   {
      double sl = NormalizeDouble(bid + slDistance, _Digits);
      double tp = NormalizeDouble(bid - tpDistance, _Digits);
      result = trade.Sell(LotSize, _Symbol, bid, sl, tp, "Pullback Sell");
      if(result)
         Print("Pullback SELL | SL: ", DoubleToString(slDistance / point, 1),
               " pips | TP: ", DoubleToString(tpDistance / point, 1), " pips");
      else
         Print("SELL failed: ", GetLastError());
   }
   if(result)
      lastTradeTime = TimeCurrent();
}
