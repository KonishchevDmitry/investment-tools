## Investment tools

This repo contains scripts which help me with managing my investments. These scripts most likely will never end up as
end user programs and published only as a response to request from some people to publish them.

Scripts are intentionally single-file to eliminate the need to install them, to simplify their modification and to
restrain their grow and gradual transformation into a full-fledged applications for which I don't have a free time to
support and evolve.

### income-statement-automizer

This script reads [Interactive Brokers](https://www.interactivebrokers.com/) statements and automatically alters *.dcX
file (Russian tax program named Декларация) by adding all required information about income from paid dividends.

*.dcX is a proprietary and very weird format which changes every year, so to make sure that everything will be done
right, script requires a fake foreign dividend income to be specified in income statement named `dividend` - it finds
it, checks that it's able to parse it and understand and only after that alters the *.dcX file.

### investments_calc.py

This module helps you in asset allocation if you want to periodically rebalance your portfolio. You create a script that
describes your current positions, expectations and free assets, run it and it suggests which positions you should
buy/sell.

Example: [investments-calc-example](investments-calc-example).