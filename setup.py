import sys
import os
def setup():
	savePath = list(sys.argv[1])

	if savePath[-1] != '/':
		savePath.append('/')
	savePath = ''.join(savePath)

	if(os.path.isdir(savePath)):

		if(input(f'WARNING EVERYTHING IN {savePath} WILL BE WIPED, CONTINUE? (YES/NO)\n').upper() == 'YES'):
			print(f'Deleting folder {savePath}\n')
			os.system('rm -r {}'.format(savePath))
			print(f'Making dir {savePath}\n')
			os.system(f'mkdir {savePath}')
		else:
			exit(0)
	else:
		print('Save path not found, creating directory\n')
		os.system(f'mkdir {savePath}')

	return savePath