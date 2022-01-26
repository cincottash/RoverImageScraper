import os
import requests
from setup import *

def getRoverPics(savePath, roverInfo):

	for rover in roverInfo:

		print(f'Creating path {savePath}{rover.name}\n')
		os.system(f'mkdir {savePath}{rover.name}')

		currentSol = 0

		while(currentSol <= rover.maxSol):
			#create the dir
			print(f'Creating path {savePath}{rover.name}/{currentSol}\n')
			os.system(f'mkdir {savePath}{rover.name}/{currentSol}')

			#download each image from each camera on each day
			for camera in rover.cameras:
				print(f'Creating path {savePath}{rover.name}/{currentSol}/{camera}\n')
				os.system(f'mkdir {savePath}{rover.name}/{currentSol}/{camera}')

				response = requests.get(f'https://mars-photos.herokuapp.com/api/v1/rovers/{rover.name}/photos?sol={currentSol}&camera={camera}')

				jsonData = response.json()

				#download all the pics taken from that camera on this sol and save it
				cameraPhotoCount = 0
				for element in jsonData['photos']:
					
					url = element['img_src']

					cmd = f'curl -L -o {savePath}{rover.name}/{currentSol}/{camera}/{cameraPhotoCount} {url}'
					print(f'{cmd}\n')
					os.system(cmd)
					cameraPhotoCount += 1

				#not every camera is used each day, so after we finish a sol, delete empty camera folders
				if cameraPhotoCount == 0:
					print(f'Deleting empty camera path {savePath}{rover.name}/{currentSol}/{camera}\n')
					os.system(f'rmdir {savePath}{rover.name}/{currentSol}/{camera}')

			#not every day has pictures taken, if there were no pics on a day, delete that day's folder
			folderCount = sum([len(folder) for r, d, folder in os.walk(f'{savePath}{rover.name}/{currentSol}')])
			
			if folderCount == 0:
				print(f'Deleting empty sol path {savePath}{rover.name}/{currentSol}\n')
				os.system(f'rmdir {savePath}{rover.name}/{currentSol}')
			

			currentSol += 1


		


def main():

	savePath, roverInfo = setup()

	getRoverPics(savePath, roverInfo)

if __name__ == '__main__':
	main()

