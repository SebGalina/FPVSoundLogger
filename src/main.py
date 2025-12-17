# Copyright (C) 2025 GALINA Sébastien <galina.sebastien@gmail.om>
# 
# This file is part of MonProjet.
#
# MonProjet is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import uasyncio as asyncio
from lib.fpv_sound_logger import FpvSoundLogger

async def main():
    fpv = FpvSoundLogger()
    fpv.start()    
    while True:
        await asyncio.sleep(1)

asyncio.run(main())
