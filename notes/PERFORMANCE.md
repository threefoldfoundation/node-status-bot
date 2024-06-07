Some notes on performance testing, particularly around the use of database indexes.

These results show the difference between using an index only on node_id, versus using a combined index over node_id and timestamp. These are all runs of a script that checks about 200 nodes for violations in a single month.

First some base lines with the non indexed data.

## No index
```
________________________________________________________
Executed in   43.47 secs    fish           external
   usr time   34.63 secs    2.15 millis   34.63 secs
   sys time    8.78 secs    0.00 millis    8.78 secs
```

### With parallel execution
```
________________________________________________________
Executed in   12.63 secs    fish           external
   usr time   51.08 secs    0.00 millis   51.08 secs
   sys time   10.88 secs    2.06 millis   10.87 secs
```

## Node ID only index
```
________________________________________________________
Executed in  526.05 millis    fish           external
   usr time  337.50 millis    1.76 millis  335.74 millis
   sys time  186.45 millis    0.22 millis  186.23 millis
________________________________________________________
Executed in  473.46 millis    fish           external
   usr time  279.25 millis  604.00 micros  278.64 millis
   sys time  192.13 millis    0.00 micros  192.13 millis
________________________________________________________
Executed in  483.93 millis    fish           external
   usr time  308.84 millis    0.00 micros  308.84 millis
   sys time  173.42 millis  617.00 micros  172.81 millis
```

### With parallel execution
```
________________________________________________________
Executed in  281.45 millis    fish           external
   usr time  597.28 millis    0.00 millis  597.28 millis
   sys time  326.33 millis    2.27 millis  324.06 millis
________________________________________________________
Executed in  263.18 millis    fish           external
   usr time  558.85 millis    0.00 millis  558.85 millis
   sys time  342.28 millis    1.13 millis  341.15 millis
________________________________________________________
Executed in  262.10 millis    fish           external
   usr time  604.57 millis  619.00 micros  603.95 millis
   sys time  327.02 millis   76.00 micros  326.95 millis
```

### File size increase
```
278M tfchain.db
234M tfchain.db.bak
```

## Combined index
```
________________________________________________________
Executed in  318.36 millis    fish           external
   usr time  226.02 millis    0.00 millis  226.02 millis
   sys time   91.44 millis    1.74 millis   89.70 millis
________________________________________________________
Executed in  288.90 millis    fish           external
   usr time  195.44 millis  516.00 micros  194.93 millis
   sys time   92.49 millis   63.00 micros   92.42 millis
________________________________________________________
Executed in  301.33 millis    fish           external
   usr time  230.53 millis  563.00 micros  229.97 millis
   sys time   70.05 millis   69.00 micros   69.98 millis
```

### With parallel execution
```
________________________________________________________
Executed in  196.26 millis    fish           external
   usr time  396.51 millis    0.00 micros  396.51 millis
   sys time  170.55 millis  763.00 micros  169.78 millis
________________________________________________________
Executed in  210.87 millis    fish           external
   usr time  401.47 millis  564.00 micros  400.91 millis
   sys time  203.61 millis   70.00 micros  203.54 millis
________________________________________________________
Executed in  195.40 millis    fish           external
   usr time  422.92 millis  627.00 micros  422.29 millis
   sys time  182.60 millis   78.00 micros  182.52 millis
```

### File size increase
```
303M tfchain.db
234M tfchain.db.bak
```

# Analysis

Using the combined index improves execution speed by about 40% over indexing only the node id. Versus the non indexed database file, the combined index increases the size by about 30% while the node id only index increases the size by about 20%.

The benefits of parallel execution are almost entirely lost with the indexed database, offering only a modest 33% increase in overall speed while bringing total overhead above the cost of serial execution.

Overall the most striking result is the reduction from 43.5 seconds of runtime to a mere .3 seconds, when comparing serial execution on the non indexed data versus on the data with combined index. That's a 145x improvement.

The storage overhead is acceptable. For our current use case we could discard old data and keep the total set at or under the 300mb used here. Even when retaining data, and even if data on more nodes were captured, this only becomes a concern on the order of several years time.

There will also be some small overhead to inserting new data with the index in place. This will almost certainly be negligible compared to the savings at read time.

For now we will just add the index whenever prepping a database file. That is, when the file is first created or any time the data ingester is started up. In the case that a large amount of data is being ingested first before any read queries are made, it would (apparently) be more efficient to create the indexes after the bulk of writes are complete. Indexing could be toggled as a CLI arg later.