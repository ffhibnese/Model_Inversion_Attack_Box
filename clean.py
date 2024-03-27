import os
import pathlib

if __name__ == '__main__':

    for p in pathlib.Path('.').rglob('*.py[co]'):
        p.unlink() 

    for p in pathlib.Path('.').rglob('__pycache__'):
        p.rmdir()
 