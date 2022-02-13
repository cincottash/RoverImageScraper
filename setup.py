import sys
import requests
from rover import *
#import os

def setup():
	savePath = sys.argv[1]

	response = requests.get('https://mars-photos.herokuapp.com/api/v1/rovers')

	jsonData = response.json()

	roverInfo = []
	for ID, rover in enumerate(jsonData['rovers']):
		name = rover['name']
		maxSol = rover['max_sol']
		totalPhotos = rover['total_photos']		
		cameras = []

		for camera in rover['cameras']:
			cameras.append(camera['name'])

		roverInfo.append(Rover(name, maxSol, totalPhotos, cameras, ID))

	print(f'Found {len(roverInfo)} rovers\n')

	maxLineLength = 26

	for rover in roverInfo:
		print(f'{rover.name}' + '.' * (maxLineLength-len(rover.name)-1) + f'{rover.ID}')	

	showInputPrompt = True

	while showInputPrompt:
		validResponse = True
		userResponse = list(input('\nPlease select which rovers to scrape\n'))
		
		#make sure they are all ints
		try:
			userResponse = [int(_) for _ in userResponse]
		except ValueError:
			print('Invalid input type, should be a single integer e.g 1234')
			validResponse = False
		
		#we cannot select more rovers than the total amount of rovers
		if validResponse:
			if len(userResponse) > len(roverInfo):
				print(f'Invalid input, cannot select more than {len(roverInfo)} rovers')
				validResponse = False

		#any num in the user response must be in the range of rover ID's
		if validResponse:
			for num in userResponse:
				if num not in range(0, len(roverInfo)):
					print(f'Invalid input, each number must be between 0 and {len(roverInfo)-1}')
					validResponse = False
					break

		if validResponse:
			showInputPrompt = False

	#"remove" any unwanted rovers
	tempRoverInfo = []
	for num in userResponse:
		for rover in roverInfo:
			if num == rover.ID:
				tempRoverInfo.append(rover)
				break
	roverInfo = tempRoverInfo

	return savePath, roverInfo