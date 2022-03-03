import os
import subprocess as sp
from setup import *

def getRoverPics(savePath, roverInfo):

	for rover in roverInfo:
		print(f'Starting rover {rover.name}\n')
		
		#check if dir exists for this rover, if not, create it
		roverNamePath = os.path.join(savePath, rover.name)
		if not os.path.isdir(roverNamePath):
			print(f'Creating path {roverNamePath}\n')
			sp.run(['mkdir', roverNamePath])
		else:
			print(f'Found existing directory for rover {rover.name} : {roverNamePath}\n')

		currentSol = 0

		while(currentSol <= rover.maxSol):
			print(f'Starting sol {currentSol} from rover {rover.name}\n')
			
			#check if dir exists for this sol, if not, create it
			solPath = os.path.join(savePath, rover.name, str(currentSol))
			if not os.path.isdir(solPath):
				
				solResponse = requests.get(f'https://mars-photos.herokuapp.com/api/v1/rovers/{rover.name}/photos?sol={currentSol}')
				solJsonData = solResponse.json()
				
				#only create sol dir if pics were taken that sol
				if len(solJsonData['photos']) > 0:
				
					print(f'Creating path {solPath}\n')
					sp.run(['mkdir', solPath])

					#download each image from each camera on each sol
					for camera in rover.cameras:
						cameraResponse = requests.get(f'https://mars-photos.herokuapp.com/api/v1/rovers/{rover.name}/photos?sol={currentSol}&camera={camera}')
						cameraJsonData = cameraResponse.json()

						#only create camera dir if there were photos taken with it that sol
						if len(cameraJsonData['photos']) > 0:
							cameraPath = os.path.join(savePath, rover.name, str(currentSol), camera)
							
							print(f'Creating path {cameraPath}\n')
							sp.run(['mkdir', cameraPath])

							for photoNumber, photo in enumerate(cameraJsonData['photos']):

								url = photo['img_src']

								#this is the final path we will save our image to
								finalPath = os.path.join(savePath, rover.name, str(currentSol), camera, str(photoNumber))
								
								cmd = f'curl -L -o {finalPath} {url}'
								print(f'{cmd}\n')
								
								sp.run(['curl', '-L', '-o', finalPath, url])
								
						else:
							print(f'No pictures taken with camera {camera} on sol {currentSol}\nSkipping camera {camera}\n')
				else:
					print(f'No pictures taken on sol {currentSol}\nSkipping sol {currentSol}\n')

			else:
				print(f'Found existing directory for sol {currentSol} : {solPath}\nSkipping sol {currentSol}\n')

			currentSol += 1



def main():

	savePath, roverInfo = setup()

	getRoverPics(savePath, roverInfo)

if __name__ == '__main__':
	main()

