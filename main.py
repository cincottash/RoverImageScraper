import os
import requests
from setup import *

def getRoverPics(savePath, roverInfo):

	for rover in roverInfo:
		print(f'Starting rover {rover.name}\n')
		
		#check if a folder already exists for this rover, if not, create it
		if not os.path.isdir(f'{savePath}{rover.name}'):
			print(f'Creating path {savePath}{rover.name}\n')
			os.system(f'mkdir {savePath}{rover.name}')
		else:
			print(f'Found existing directory for rover {rover.name} : {savePath}{rover.name}\n')

		currentSol = 0

		while(currentSol <= rover.maxSol):
			print(f'Starting sol {currentSol} from rover {rover.name}\n')
			
			#check if dir already exists for this sol
			if not os.path.isdir(f'{savePath}{rover.name}/{currentSol}'):
				
				#if it doesn't exist, also check if there were any pics taken on this day
				solResponse = requests.get(f'https://mars-photos.herokuapp.com/api/v1/rovers/{rover.name}/photos?sol={currentSol}')
				solJsonData = solResponse.json()
				
				if len(solJsonData['photos']) > 0:
				
					#create the dir
					print(f'Creating path {savePath}{rover.name}/{currentSol}\n')
					os.system(f'mkdir {savePath}{rover.name}/{currentSol}')

					#download each image from each camera on each day
					for camera in rover.cameras:
						cameraResponse = requests.get(f'https://mars-photos.herokuapp.com/api/v1/rovers/{rover.name}/photos?sol={currentSol}&camera={camera}')
						cameraJsonData = cameraResponse.json()

						print(f'Creating path {savePath}{rover.name}/{currentSol}/{camera}\n')
						os.system(f'mkdir {savePath}{rover.name}/{currentSol}/{camera}')

						#download all the pics taken from that camera on this sol and save it
						cameraPhotoCount = 0
						if len(cameraJsonData['photos']) > 0:
							for photo in cameraJsonData['photos']:
								url = photo['img_src']

								cmd = f'curl -L -o {savePath}{rover.name}/{currentSol}/{camera}/{cameraPhotoCount} {url}'
								print(f'{cmd}\n')
								os.system(cmd)
								
								cameraPhotoCount += 1
						else:
							print(f'No pictures taken with camera {camera} on sol {currentSol}\nSkipping camera {camera}\n')
				else:
					print(f'No pictures taken on sol {currentSol}\nSkipping sol {currentSol}\n')

			else:
				print(f'Found existing directory for sol {currentSol} : {savePath}{rover.name}/{currentSol}\nSkipping sol {currentSol}\n')

			currentSol += 1


	
def main():

	savePath, roverInfo = setup()

	getRoverPics(savePath, roverInfo)

if __name__ == '__main__':
	main()

