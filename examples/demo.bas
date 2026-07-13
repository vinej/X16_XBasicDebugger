' X16 XBasic debugger demo
' The shared test program for breakpoints, stepping and variables.
' Compiles for -t x16 with the debug-info fork of xcbasic3.

DIM total AS LONG
DIM count AS BYTE
DIM msg AS STRING * 16

total = 0
count = 0
msg = "sum ="

FOR i AS LONG = 1 TO 10
  total = total + i
  count = count + 1
  PRINT i; total
NEXT i

PRINT msg; total
PRINT "count ="; count
END
