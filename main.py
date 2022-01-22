import os
import requests
import sys

from setup import *
from rover import *

def getRoverInfo():
	r = requests.get('https://mars-photos.herokuapp.com/api/v1/rovers')

	jsonData = r.json()

	roverInfo = []

	for element in jsonData['rovers']:
		name = element['name']
		maxSol = element['max_sol']
		totalPhotos = element['total_photos']		
		cameras = []

		for camera in element['cameras']:
			cameras.append(camera['name'])

		roverInfo.append(Rover(name, maxSol, totalPhotos, cameras))

	return roverInfo


def getRoverPics(savePath, roverInfo):
	for rover in roverInfo:
		print(f'Creating dir: {savePath}{rover.name}\n')
		os.system(f'mkdir {savePath}{rover.name}')

		#print('name: {}\ncameras:{}\n\n'.format(rover.name, rover.cameras))
		currentSol = 0

		while(currentSol <= rover.maxSol):
			#create the dir
			print(f'Creating dir: {savePath}{rover.name}/{currentSol}\n')
			os.system(f'mkdir {savePath}{rover.name}/{currentSol}')

			#download each image from each camera on each day
			for camera in rover.cameras:
				print(f'Creating dir: {savePath}{rover.name}/{currentSol}/{camera}\n')
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

				#delete empty camera folders
				if cameraPhotoCount == 0:
					print(f'Deleting empty camera folder: {savePath}{rover.name}/{currentSol}/{camera}\n')
					os.system(f'rmdir {savePath}{rover.name}/{currentSol}/{camera}')

			#delete empty sol folders
			#if there are no subfolders in this sol's dir, we can delete that sol's dir
			folderCount = sum([len(folder) for r, d, folder in os.walk(f'{savePath}{rover.name}/{currentSol}')])
			
			if folderCount == 0:
				print(f'Deleting empty sol folder: {savePath}{rover.name}/{currentSol}\n')
				os.system(f'rmdir {savePath}{rover.name}/{currentSol}')
			

			currentSol += 1


		


def main():

	savePath = setup()

	roverInfo = getRoverInfo()

	getRoverPics(savePath, roverInfo)

if __name__ == '__main__':
	main()

