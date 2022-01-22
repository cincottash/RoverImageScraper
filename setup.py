import sys
import os
def setup():
	savePath = list(sys.argv[1])

	if savePath[len(savePath) -1 ] != '/':
		savePath.append('/')
	savePath = ''.join(savePath)
	
	if(os.path.isdir(savePath)):

		if(input('WARNING EVERYTHING IN {} WILL BE WIPED, CONTINUE? (YES/NO)\n'.format(savePath)).upper() == 'YES'):
			print('Deleting folder {}\n'.format(savePath))
			os.system('rm -r {}'.format(savePath))
			print('Making dir {}\n'.format(savePath))
			os.system('mkdir {}'.format(savePath))
		else:
			exit(0)
	else:
		print('Save path not found, creating directory\n')
		os.system('mkdir {}'.format(savePath))

	return savePath