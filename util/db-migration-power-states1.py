# This script migrates a db produced before commit 5691583f81 to incorporate the change that we also want to store the timestamps of blocks when nodes went to sleep when fetching the initial power states/targets

# It has two separate steps, invoked with "fetch" and "replace". The first one retrieves the new form of data and stores it in an intermediary database. The second one performs the actual replacement into the main database. The fetch command can be used on a running database. Then bring the bot down, run the replace command in this script, and bring up a version of the bot with the new code that works on the new data

import sys, os, sqlite3
from multiprocessing import Process
sys.path.append('../')
import ingester

NEWDB = "migration-data.db"

command = sys.argv[1]
dbfile = sys.argv[2]

if command == "fetch":
    if os.path.isfile(NEWDB):
        print('New db file already exists ({}). Please move it and try again. Exiting.'.format(NEWDB))
        exit()
    con = sqlite3.connect(dbfile)
    connew = sqlite3.connect(NEWDB)

    # Technically we just need the one table, but this is easy
    ingester.prep_db(connew)

    blocks = con.execute('SELECT DISTINCT block FROM PowerState').fetchall()
    blocks = [b[0] for b in blocks]

    print('Found existing power state informaiton at these blocks:', blocks)

    for block in blocks:
        t = Process(target=ingester.fetch_powers, args=[block, NEWDB])
        t.start()

elif command == "replace":
    if os.path.isfile(dbfile + '.bak'):
        print('Backup file already exists for this database. Please move it and try again. Exiting.')
        exit()

    con = sqlite3.connect(dbfile)
    connew = sqlite3.connect(NEWDB)
    conback = sqlite3.connect(dbfile + '.bak')

    con.backup(conback)
    conback.close()

    con.execute('DROP TABLE PowerState')
    ingester.prep_db(con)

    con.executemany("INSERT INTO PowerState VALUES(?, ?, ?, ?, ?, ?, ?)", connew.execute('SELECT * FROM PowerState').fetchall())
    con.commit()
    con.close()
    connew.close()