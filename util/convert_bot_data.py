"""
This script converts bot data pickle files that were generated with the original "grid_types" module to use the new style "grid3.types"
"""

import shutil, sys, pickle, os.path
import grid3.types

if os.path.exists('bot_data.bak'):
    print("Can't backup to bot_data.bak because it already exists. Please move or remove that file and run again.")
else:
    sys.modules['grid_types'] = grid3.types
    shutil.copyfile('bot_data', 'bot_data.bak')
    data = pickle.load(open('bot_data', 'rb'))
    pickle.dump(data, open('bot_data', 'wb'))