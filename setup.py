import sys
import os
import requests
from rover import *

def setup():
	savePath = list(sys.argv[1])

	if savePath[-1] != '/':
		savePath.append('/')
	savePath = ''.join(savePath)

	response = requests.get('https://mars-photos.herokuapp.com/api/v1/rovers')

	jsonData = response.json()

	roverInfo = []
	ID = 0
	for element in jsonData['rovers']:
		#print(ID)
		name = element['name']
		maxSol = element['max_sol']
		totalPhotos = element['total_photos']		
		cameras = []

		for camera in element['cameras']:
			cameras.append(camera['name'])

		roverInfo.append(Rover(name, maxSol, totalPhotos, cameras, ID))
		ID += 1

	print(f'Found {len(roverInfo)} rovers\n')

	maxLineLength = 26


	for rover in roverInfo:
		print(f'{rover.name}' + '.' * (maxLineLength-len(rover.name)-1) + f'{rover.ID}')
	print('All' + '.' * (maxLineLength-len('All')-1) + f'{rover.ID+1}')
	

	showInputPrompt = True

	while showInputPrompt:
		validResponse = True
		userResponse = list(input('Please select which rovers to scrape\n'))
		
		#make sure they are all ints
		try:
			userResponse = [int(_) for _ in userResponse]
		except ValueError:
			print('Invalid input, should be a single number e.g 1234\n')
			validResponse = False
			

		if validResponse:
			userResponse.sort()

			for char in userResponse:
				if int(char) not in range(0, rover.ID+2):
					print('Invalid input, should be a single number e.g 1234\n')
					validResponse = False
					break

		#we also shouldn't be allowed to pick the last option (All) and any other individual rovers
		if validResponse and int(userResponse[-1]) == rover.ID+1 and len(userResponse) > 1:
			print(f'Invalid input, if choosing All rovers, no other rovers must be specified\n')
			validResponse = False


		if validResponse:
			showInputPrompt = False

	tempRoverInfo = []
	if userResponse[-1] != len(roverInfo):
		for rover in roverInfo:
			
			if rover.ID in userResponse:
				tempRoverInfo.append(rover)

		roverInfo = tempRoverInfo

	print(f'WARNING, EVERYTHING IN THE FOLLOWING DIRECTORIES WILL BE DELETED\n')
	for rover in roverInfo:
		if(os.path.isdir(f'{savePath}{rover.name}')):
			print(f'{savePath}{rover.name}\n')

	if(input(f'CONTINUE? (YES/NO)\n').upper() == 'YES'):
		for rover in roverInfo:
			if(os.path.isdir(f'{savePath}{rover.name}')):

				print(f'Deleting path {savePath}{rover.name}\n')
				os.system(f'rm -r {savePath}{rover.name}')

	else:
		exit(0)

	return savePath, roverInfo