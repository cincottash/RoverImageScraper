import sys
import requests
from rover import *
import os

def setup():

	if not os.path.isdir(sys.argv[1]):
		print(f'Error, directory {sys.argv[1]} does not exist\n')
		exit(0)
	else:
		savePath = sys.argv[1]

	roverResponse = requests.get('https://mars-photos.herokuapp.com/api/v1/rovers')

	roverJsonData = roverResponse.json()

	roverInfo = []
	for ID, rover in enumerate(roverJsonData['rovers']):
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
		validroverResponse = True
		userroverResponse = list(input('\nPlease select which rovers to scrape\n'))
		
		#make sure they are all ints
		try:
			userroverResponse = [int(_) for _ in userroverResponse]
		except ValueError:
			print('Invalid input type, should be a single integer e.g 1234')
			validroverResponse = False
		
		#we cannot select more rovers than the total amount of rovers
		if validroverResponse:
			if len(userroverResponse) > len(roverInfo):
				print(f'Invalid input, cannot select more than {len(roverInfo)} rovers')
				validroverResponse = False

		#any num in the user roverResponse must be in the range of rover ID's
		if validroverResponse:
			for num in userroverResponse:
				if num not in range(0, len(roverInfo)):
					print(f'Invalid input, each number must be between 0 and {len(roverInfo)-1}')
					validroverResponse = False
					break

		if validroverResponse:
			showInputPrompt = False

	#"remove" any unwanted rovers
	tempRoverInfo = []
	for num in userroverResponse:
		for rover in roverInfo:
			if num == rover.ID:
				tempRoverInfo.append(rover)
				break
	roverInfo = tempRoverInfo

	return savePath, roverInfo